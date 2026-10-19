"""Set up a plant: its description (``plant/2``, pasted or built), what it measures, and
where each input comes from.

Each section is checked by the library's own readers, every problem named, and each
forecast source is read once before its step is accepted. The options edit each part,
and the engine's settings (``engine/2``).
"""

from __future__ import annotations

import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import probatio as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.entity import Kind
from microgrid.cli.sources.read import read_forecast
from microgrid.plant import Plant

from .const import (
    CONF_ENGINE,
    CONF_LOAD_FORECAST,
    CONF_MEASURED,
    CONF_PLANT,
    CONF_PRICES,
    CONF_PV_FORECAST,
    DOMAIN,
    SOURCE_SECTIONS,
)
from .discovery import EnergyFound, changed_signs, energy_found, proposed_signs, with_signs
from .found_sources import Found, found_sources
from .hass_api import HassApi
from .setup import (
    SetupError,
    read_engine,
    read_measured,
    read_plant,
    read_section,
    read_setup,
)

CONF_NAME = "name"
CONF_PLANT_TOML = "plant_toml"
CONF_SOURCES_TOML = "sources_toml"
CONF_FROM = "from"
NO_SOURCE = "none"
#: A choice of a source found: its index among those offered.
FOUND = "found:"

# -- the fields of each source -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Field:
    """One setting of a source's section, as a form field."""

    key: str
    kind: str  # entity, entities, any_entity, text, number, minutes, object, path, entry:<domain>
    required: bool = False


_ROWS = (
    Field("start_key", "text"),
    Field("end_key", "text"),
    Field("period_minutes", "minutes"),
)
_PRICE_ROWS = (
    *_ROWS,
    Field("import_key", "text", required=True),
    Field("export_key", "text"),
    Field("unit", "text"),
    Field("export", "number"),
)
_POWER_ROWS = (
    *_ROWS,
    Field("value_key", "text"),
    Field("p10_key", "text"),
    Field("p50_key", "text"),
    Field("p90_key", "text"),
    Field("unit", "text"),
)
_ACTION = (
    Field("action", "text", required=True),
    Field("data", "object"),
    Field("rows", "path"),
)

#: Each input's readers, and each reader's fields.
READERS: dict[str, dict[str, tuple[Field, ...]]] = {
    CONF_PRICES: {
        "tariffkit": (Field("config_entry", "entry:tariffkit"),),
        "nordpool": (
            Field("area", "text", required=True),
            Field("export", "number", required=True),
            Field("config_entry", "entry:nordpool"),
            Field("currency", "text"),
        ),
        "amber": (
            Field("price", "entity", required=True),
            Field("forecast", "entity", required=True),
            Field("feed_in_price", "entity"),
            Field("feed_in_forecast", "entity"),
            Field("export", "number"),
        ),
        "tibber": (Field("export", "number", required=True), Field("home", "text")),
        "octopus": (
            Field("entities", "entities", required=True),
            Field("export_entities", "entities"),
            Field("export", "number"),
        ),
        "entsoe": (
            Field("entity", "any_entity", required=True),
            Field("export", "number", required=True),
        ),
        "openei": (
            Field("current_rate", "entity", required=True),
            Field("next_structure_time", "entity", required=True),
            Field("adjustment", "entity"),
            Field("sell_rate", "entity"),
            Field("export", "number"),
        ),
        "entity": (
            Field("entity", "any_entity", required=True),
            Field("attribute", "text", required=True),
            *_PRICE_ROWS,
        ),
        "action": (*_ACTION, *_PRICE_ROWS),
    },
    CONF_PV_FORECAST: {
        "solcast": (
            Field("entities", "entities", required=True),
            Field("last_polled", "entity", required=True),
        ),
        "entity": (
            Field("entity", "any_entity", required=True),
            Field("attribute", "text", required=True),
            *_POWER_ROWS,
        ),
        "action": (*_ACTION, *_POWER_ROWS),
    },
    CONF_LOAD_FORECAST: {
        "emhass": (
            Field("entity", "entity"),
            Field("attribute", "text"),
            Field("value_key", "text"),
            Field("period_minutes", "minutes"),
        ),
        "predbat": (
            Field("entity", "any_entity"),
            Field("attribute", "text"),
            Field("step_minutes", "minutes"),
        ),
        "entity": (
            Field("entity", "any_entity", required=True),
            Field("attribute", "text", required=True),
            *_POWER_ROWS,
        ),
        "action": (*_ACTION, *_POWER_ROWS),
    },
}

#: The readers whose rows' keys are a ``keys`` table (EMHASS names its one key itself).
_KEYED_READERS = ("entity", "action")
#: The form's keys that become the section's ``keys`` table.
_KEY_FIELDS = {
    "import_key": "import",
    "export_key": "export",
    "value_key": "value",
    "p10_key": "p10",
    "p50_key": "p50",
    "p90_key": "p90",
}


def _selector(kind: str) -> Any:
    if kind == "entity":
        return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    if kind == "any_entity":
        return selector.EntitySelector()
    if kind == "entities":
        return selector.EntitySelector(selector.EntitySelectorConfig(multiple=True))
    if kind in ("number", "minutes"):
        return selector.NumberSelector(
            selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step="any")
        )
    if kind == "object":
        return selector.ObjectSelector()
    if kind.startswith("entry:"):
        return selector.ConfigEntrySelector(
            selector.ConfigEntrySelectorConfig(integration=kind.partition(":")[2])
        )
    return selector.TextSelector()


def source_schema(name: str, reader: str, current: Mapping[str, Any]) -> vol.Schema:
    """The form for one reader's fields, filled from ``current`` (its section)."""
    shown = form_values(name, reader, current)
    schema: dict[Any, Any] = {}
    for spec in READERS[name][reader]:
        default = shown.get(spec.key)
        marker = vol.Required if spec.required else vol.Optional
        key = (
            marker(spec.key, description={"suggested_value": default})
            if default is not None
            else marker(spec.key)
        )
        schema[key] = _selector(spec.kind)
    return vol.Schema(schema)


def section_from_form(name: str, reader: str, form: Mapping[str, Any]) -> dict[str, Any]:
    """A reader's form as its ``sources/1`` section."""
    section: dict[str, Any] = {CONF_FROM: reader}
    keys: dict[str, str] = {}
    for spec in READERS[name][reader]:
        value = form.get(spec.key)
        if value in (None, "", [], {}):
            continue
        if spec.key in _KEY_FIELDS and reader in _KEYED_READERS:
            keys[_KEY_FIELDS[spec.key]] = str(value)
        elif spec.kind == "path":
            section[spec.key] = [part for part in str(value).split(".") if part]
        elif spec.kind == "minutes":
            section[spec.key] = float(value) if float(value) % 1 else int(value)
        elif spec.kind == "number":
            section[spec.key] = float(value)
        else:
            section[spec.key] = value
    if keys:
        section["keys"] = keys
    return section


def form_values(name: str, reader: str, section: Mapping[str, Any]) -> dict[str, Any]:
    """A section as its reader's form's values."""
    if section.get(CONF_FROM) != reader:
        return {}
    found: dict[str, Any] = {}
    keys = section.get("keys")
    named = keys if isinstance(keys, Mapping) else {}
    for spec in READERS[name][reader]:
        if spec.key in _KEY_FIELDS and reader in _KEYED_READERS:
            if _KEY_FIELDS[spec.key] in named:
                found[spec.key] = named[_KEY_FIELDS[spec.key]]
        elif spec.key in section:
            value = section[spec.key]
            found[spec.key] = ".".join(value) if spec.kind == "path" else value
    return found


def _problems(err: SetupError) -> dict[str, str]:
    return {"problems": "\n".join(f"- {problem}" for problem in err.problems)}


async def _try_source(
    hass: HomeAssistant,
    plant: Plant,
    engine: Mapping[str, Any] | None,
    name: str,
    section: Mapping[str, Any],
) -> dict[str, str] | None:
    """Read the source once; the placeholders naming why it cannot be read, else None."""
    source = read_section(name, section)
    config = read_engine(engine, plant)
    kinds: dict[str, Kind] = {
        CONF_PRICES: "prices",
        CONF_PV_FORECAST: "pv_forecast",
        CONF_LOAD_FORECAST: "load_forecast",
    }
    try:
        await read_forecast(
            HassApi(hass),
            source,
            kinds[name],
            config,
            dt_util.utcnow(),
            tz=plant.config.tz,
        )
    except (SourceError, ValueError) as err:
        return {"error": str(err)}
    return None


# -- the plant -------------------------------------------------------------------------------


def plant_from_toml(text: str) -> dict[str, Any]:
    """The plant's TOML as a mapping. Raises :class:`SetupError`."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise SetupError([f"not TOML: {err}"]) from err


def plant_to_toml(data: Mapping[str, Any]) -> str:
    """A plant mapping as TOML text, for editing."""
    from microgrid.cli.toml_text import dumps

    return dumps(dict(data))


def sources_from_toml(text: str) -> dict[str, Any]:
    """A ``sources/1`` file's sections, as the entry's options: the sources and what is
    measured. Its ``home_assistant`` section and its files' paths are the command's;
    here Home Assistant is the one running. Raises :class:`SetupError`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise SetupError([f"not TOML: {err}"]) from err
    if data.get("schema") != "sources/1":
        raise SetupError(['schema: must be "sources/1"'])
    return {
        name: data[name]
        for name in (*SOURCE_SECTIONS, CONF_MEASURED)
        if isinstance(data.get(name), Mapping)
    }


_COUPLINGS = ["none", "ac", "dc"]
_CONNECTIONS = ["none", "ac", "dc_bus"]


def built_plant(
    choices: Mapping[str, Any], battery: Mapping[str, Any], grid: Mapping[str, Any]
) -> dict[str, Any]:
    """A plant mapping from the builder's answers."""
    plant: dict[str, Any] = {"schema": "plant/2"}
    coupling = choices.get("pv", "none")
    connection = choices.get("battery", "none")
    if coupling != "none":
        plant["pv"] = [{"name": "pv", "coupling": coupling}]
    hybrid = coupling == "dc" or connection == "dc_bus" or choices.get("dc_load", False)
    inverter = {
        key: float(battery[key])
        for key in ("standby_kw", "working_kw", "ac_rating_kw")
        if battery.get(key) is not None
    }
    if connection != "none":
        part: dict[str, Any] = {
            key: float(battery[key])
            for key in (
                "capacity_kwh",
                "charge_kw",
                "discharge_kw",
                "eta_charge",
                "eta_discharge",
                "reserve_soc",
                "wear_charge_per_kwh",
                "wear_discharge_per_kwh",
            )
            if battery.get(key) is not None
        }
        part["connection"] = connection
        if connection == "ac":
            part["inverter"] = inverter
        plant["battery"] = part
    if hybrid:
        plant["hybrid"] = inverter if connection == "dc_bus" else {}
    if choices.get("dc_load", False):
        plant["loads"] = [{"name": "house"}, {"name": "dc_load", "connection": "dc_bus"}]
    limits = {
        key: float(grid[key])
        for key in ("import_limit_kw", "export_limit_kw")
        if grid.get(key) is not None
    }
    if limits:
        plant["grid"] = limits
    return plant


def _number(required: bool = False) -> Any:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step="any", min=0)
    )


_BATTERY_SCHEMA = vol.Schema(
    {
        vol.Required("capacity_kwh"): _number(),
        vol.Required("charge_kw"): _number(),
        vol.Required("discharge_kw"): _number(),
        vol.Optional("eta_charge"): _number(),
        vol.Optional("eta_discharge"): _number(),
        vol.Optional("reserve_soc"): _number(),
        vol.Optional("wear_charge_per_kwh"): _number(),
        vol.Optional("wear_discharge_per_kwh"): _number(),
        vol.Optional("ac_rating_kw"): _number(),
        vol.Optional("standby_kw"): _number(),
        vol.Optional("working_kw"): _number(),
    }
)
_GRID_SCHEMA = vol.Schema(
    {vol.Optional("import_limit_kw"): _number(), vol.Optional("export_limit_kw"): _number()}
)


# -- what is measured ------------------------------------------------------------------------

#: The SOC's unit where its entity states none: a fraction (0 to 1), ``soc_unit = ""``.
_SOC_FRACTION = "fraction"
#: The SOC in percent where its entity states no unit, ``soc_unit = "%"`` (the default).
_SOC_PERCENT = "percent"


def measured_schema(plant: Plant, current: Mapping[str, Any]) -> vol.Schema:
    """The measured entities' form: one per plant meter, the SOC, the house load, the mode."""
    meters = current.get("meters", {}) if isinstance(current.get("meters"), Mapping) else {}
    power = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    schema: dict[Any, Any] = {}

    def optional(key: str, value: Any) -> Any:
        return (
            vol.Optional(key, description={"suggested_value": value})
            if value is not None
            else vol.Optional(key)
        )

    for name in plant.meters:
        schema[optional(f"meter_{name}", meters.get(name))] = power
    if plant.storage is not None:
        schema[optional("soc", current.get("soc"))] = power
        schema[
            vol.Required(
                "soc_unit", default=_SOC_FRACTION if current.get("soc_unit") == "" else _SOC_PERCENT
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[_SOC_PERCENT, _SOC_FRACTION], translation_key="soc_unit"
            )
        )
    schema[optional("house", current.get("house"))] = power
    if plant.storage is not None:
        schema[optional("dispatch", current.get("dispatch"))] = selector.EntitySelector()
        schema[optional("modelled_modes", ", ".join(current.get("modelled_modes", [])) or None)] = (
            selector.TextSelector()
        )
        schema[
            vol.Optional(
                "dispatch_as_modelled",
                default=current.get("dispatch_as_modelled", True) is not False,
            )
        ] = selector.BooleanSelector()
    schema[optional("heartbeat", current.get("heartbeat"))] = selector.EntitySelector()
    return vol.Schema(schema)


def measured_from_form(plant: Plant, form: Mapping[str, Any]) -> dict[str, Any]:
    """The measured form as its ``sources/1`` section."""
    section: dict[str, Any] = {CONF_FROM: "home_assistant"}
    meters = {name: form[f"meter_{name}"] for name in plant.meters if form.get(f"meter_{name}")}
    if meters:
        section["meters"] = meters
    for key in ("soc", "house", "heartbeat"):
        if form.get(key):
            section[key] = form[key]
    if plant.storage is not None:
        if form.get("soc_unit") == _SOC_FRACTION:
            section["soc_unit"] = ""
        if form.get("dispatch"):
            section["dispatch"] = form["dispatch"]
            modes = [m.strip() for m in str(form.get("modelled_modes", "")).split(",") if m.strip()]
            section["modelled_modes"] = modes
        else:
            section["dispatch_as_modelled"] = bool(form.get("dispatch_as_modelled", True))
    return section


def check_measured(plant: Plant, section: Mapping[str, Any]) -> None:
    """Raise :class:`SetupError` for what the plant needs and the section lacks."""
    problems: list[str] = []
    if plant.storage is not None and not section.get("soc"):
        problems.append("soc: a plant with a battery needs its SOC")
    try:
        read_measured(section)
    except SetupError as err:
        problems.extend(err.problems)
    if problems:
        raise SetupError(problems)


# -- the flows -------------------------------------------------------------------------------


def energy_section(plant: Plant, found: EnergyFound) -> dict[str, Any]:
    """The measured section the Energy dashboard proposes for ``plant``."""
    by_kind = {"pv": found.pv, "grid": found.grid, "battery": found.battery}
    kinds = [meter.measures for meter in plant.config.meter_set]
    meters = {
        meter.name: entity
        for meter in plant.config.meter_set
        if (entity := by_kind.get(meter.measures)) is not None and kinds.count(meter.measures) == 1
    }
    section: dict[str, Any] = {"meters": meters} if meters else {}
    if found.soc is not None and plant.storage is not None:
        section["soc"] = found.soc
    return section


def plant_id(plant: Plant, measured: Mapping[str, Any]) -> str | None:
    """What a plant is known by: its grid meter's entity, else its consumption
    meter's (the meter in the grid's place)."""
    meters = measured.get("meters", {})
    for kind in ("grid", "consumption"):
        for meter in plant.config.meter_set:
            if meter.measures == kind and meters.get(meter.name):
                return str(meters[meter.name])
    return None


def _notes(lines: tuple[str, ...]) -> dict[str, str]:
    return {"notes": "\n".join(f"- {line}" for line in lines)}


class _Sections:
    """The steps the config and options flows share."""

    hass: HomeAssistant
    plant_data: dict[str, Any]
    options: dict[str, Any]
    _reader: dict[str, str]
    #: The signs to confirm that differ from the plant's, each with why.
    _signs: dict[str, tuple[float, str]] | None = None
    #: The sources found for each input, as last offered.
    _found: dict[str, list[Found]]

    def _plant(self) -> Plant:
        return read_plant(self.plant_data, self.hass.config.time_zone)

    def _keep_plant(self, data: dict[str, Any]) -> None:
        raise NotImplementedError

    async def _after_measured(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def _measured_suggested(self, plant: Plant) -> dict[str, Any]:
        """What the measured form shows: the section as set, else the Energy
        dashboard's proposal."""
        current = self.options.get(CONF_MEASURED)
        if current:
            return dict(current)
        return energy_section(plant, await energy_found(self.hass))

    async def _measured_kept(self, plant: Plant, section: dict[str, Any]) -> ConfigFlowResult:
        """Keep the measured section; confirm the signs the recorder proposes, if any
        differ from the plant's."""
        self.options[CONF_MEASURED] = section
        changed = changed_signs(plant, await proposed_signs(self.hass, plant, section))
        if changed:
            self._signs = changed
            return await self.async_step_signs()
        return await self._after_measured()

    async def async_step_signs(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The meters whose recorded readings say they read the other way."""
        assert self._signs is not None
        changed = self._signs
        if user_input is not None:
            if user_input.get("apply", True):
                signs = {name: sign for name, (sign, _) in changed.items()}
                self._keep_plant(with_signs(self.plant_data, self._plant(), signs))
            self._signs = None
            return await self._after_measured()
        notes = tuple(f"{why}: sign {sign:+.0f}" for sign, why in changed.values())
        return self.async_show_form(  # type: ignore[attr-defined,no-any-return]
            step_id="signs",
            data_schema=vol.Schema(
                {vol.Required("apply", default=True): selector.BooleanSelector()}
            ),
            description_placeholders=_notes(notes),
        )

    async def _choose(
        self, name: str, user_input: dict[str, Any] | None, *, optional: bool
    ) -> tuple[str | None, vol.Schema]:
        """The source chosen for ``name``: one an installed integration publishes
        (``found:<n>``), or a reader to set up by hand. Without a source set, the first
        found is proposed."""
        if user_input is not None:
            return str(user_input[CONF_FROM]), vol.Schema({})
        found = self._found[name] = found_sources(self.hass, name)
        readers = [*READERS[name], *([NO_SOURCE] if optional else [])]
        options: list[selector.SelectOptionDict] = [
            selector.SelectOptionDict(value=f"{FOUND}{i}", label=f.label)
            for i, f in enumerate(found)
        ]
        options += [selector.SelectOptionDict(value=r, label=r) for r in readers]
        current = self.options.get(name, {}).get(CONF_FROM)
        if current is None:
            current = f"{FOUND}0" if found else (NO_SOURCE if optional else readers[0])
        schema = vol.Schema(
            {
                vol.Required(CONF_FROM, default=current): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, translation_key=f"{name}_reader")
                )
            }
        )
        return None, schema

    def _pick(self, name: str, chosen: str) -> tuple[str, dict[str, Any] | None]:
        """The reader ``chosen`` names; for a source found, its section kept to fill the
        reader's form, and the form's values to submit at once where it needs nothing
        more."""
        if not chosen.startswith(FOUND):
            return chosen, None
        found = self._found[name][int(chosen.removeprefix(FOUND))]
        reader = str(found.section[CONF_FROM])
        self.options[name] = dict(found.section)
        return reader, None if found.missing else form_values(name, reader, found.section)

    async def _fields(
        self, name: str, user_input: dict[str, Any] | None
    ) -> tuple[dict[str, str], dict[str, str], bool]:
        """Check a reader's form: (errors, placeholders, accepted)."""
        if user_input is None:
            return {}, {}, False
        reader = self._reader[name]
        section = section_from_form(name, reader, user_input)
        try:
            plant = self._plant()
            failed = await _try_source(
                self.hass, plant, self.options.get(CONF_ENGINE), name, section
            )
        except SetupError as err:
            return {"base": "invalid"}, _problems(err), False
        if failed is not None:
            return {"base": "read_failed"}, failed, False
        self.options[name] = section
        return {}, {}, True


class MicrogridConfigFlow(_Sections, ConfigFlow, domain=DOMAIN):
    """The plant, what it measures, and its sources."""

    VERSION = 1

    def __init__(self) -> None:
        self.plant_data = {}
        self.options = {CONF_ENGINE: {}}
        self._reader = {}
        self._found = {}
        self._title = "Microgrid"
        self._choices: dict[str, Any] = {}
        self._battery: dict[str, Any] = {}
        #: Whether the sources came from a file: once measured, the entry is made.
        self._from_files = False

    def _keep_plant(self, data: dict[str, Any]) -> None:
        self.plant_data = data

    async def _after_measured(self) -> ConfigFlowResult:
        if self._from_files:
            return self._create()
        return await self.async_step_prices()

    async def _unique(self, plant: Plant, section: Mapping[str, Any]) -> None:
        """One entry per plant, known by its grid meter's entity (:func:`plant_id`)."""
        if (found := plant_id(plant, section)) is not None:
            await self.async_set_unique_id(found)
            self._abort_if_unique_id_configured()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MicrogridOptionsFlow:
        return MicrogridOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="user", menu_options=["plant_build", "plant_paste", "files"]
        )

    async def async_step_files(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A plant file and a ``sources/1`` file, as the command reads them."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                plant_data = plant_from_toml(user_input[CONF_PLANT_TOML])
                options = sources_from_toml(user_input[CONF_SOURCES_TOML])
                if CONF_PRICES not in options:
                    raise SetupError(["prices: the sources file names no prices"])
                setup = read_setup({CONF_PLANT: plant_data}, options, self.hass.config.time_zone)
                check_measured(setup.plant, options.get(CONF_MEASURED, {}))
                failed = [
                    f"{name}: {why['error']}"
                    for name in SOURCE_SECTIONS
                    if name in options
                    and (
                        why := await _try_source(self.hass, setup.plant, None, name, options[name])
                    )
                    is not None
                ]
            except SetupError as err:
                errors["base"], placeholders = "invalid", _problems(err)
            else:
                if failed:
                    errors["base"], placeholders = "read_failed", {"error": "; ".join(failed)}
                else:
                    self._title = user_input[CONF_NAME]
                    self.plant_data = plant_data
                    self.options = {**options, CONF_ENGINE: {}}
                    self._from_files = True
                    measured = dict(options.get(CONF_MEASURED, {}))
                    await self._unique(setup.plant, measured)
                    return await self._measured_kept(setup.plant, measured)
        multiline = selector.TextSelector(selector.TextSelectorConfig(multiline=True))
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self._title): selector.TextSelector(),
                vol.Required(CONF_PLANT_TOML): multiline,
                vol.Required(CONF_SOURCES_TOML): multiline,
            }
        )
        return self.async_show_form(
            step_id="files",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_plant_paste(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                data = plant_from_toml(user_input[CONF_PLANT_TOML])
                read_plant(data, self.hass.config.time_zone)
            except SetupError as err:
                errors["base"], placeholders = "invalid", _problems(err)
            else:
                self._title = user_input[CONF_NAME]
                self.plant_data = data
                return await self.async_step_measured()
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self._title): selector.TextSelector(),
                vol.Required(CONF_PLANT_TOML): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
            }
        )
        return self.async_show_form(
            step_id="plant_paste",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_plant_build(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._title = user_input[CONF_NAME]
            self._choices = user_input
            if user_input["battery"] != "none":
                return await self.async_step_plant_battery()
            return await self.async_step_plant_grid()
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self._title): selector.TextSelector(),
                vol.Required("pv", default="ac"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=_COUPLINGS, translation_key="pv_coupling")
                ),
                vol.Required("battery", default="ac"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_CONNECTIONS, translation_key="battery_connection"
                    )
                ),
                vol.Required("dc_load", default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="plant_build", data_schema=schema)

    async def async_step_plant_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._battery = user_input
            return await self.async_step_plant_grid()
        capacity = (await energy_found(self.hass)).capacity_kwh
        return self.async_show_form(
            step_id="plant_battery",
            data_schema=self.add_suggested_values_to_schema(
                _BATTERY_SCHEMA, {"capacity_kwh": capacity} if capacity else {}
            ),
        )

    async def async_step_plant_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            data = built_plant(self._choices, self._battery, user_input)
            try:
                read_plant(data, self.hass.config.time_zone)
            except SetupError as err:
                errors["base"], placeholders = "invalid", _problems(err)
            else:
                self.plant_data = data
                return await self.async_step_measured()
        return self.async_show_form(
            step_id="plant_grid",
            data_schema=_GRID_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_measured(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        plant = self._plant()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            section = measured_from_form(plant, user_input)
            try:
                check_measured(plant, section)
            except SetupError as err:
                errors["base"], placeholders = "invalid", _problems(err)
            else:
                await self._unique(plant, section)
                return await self._measured_kept(plant, section)
        return self.async_show_form(
            step_id="measured",
            data_schema=measured_schema(plant, await self._measured_suggested(plant)),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_prices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        chosen, schema = await self._choose(CONF_PRICES, user_input, optional=False)
        if chosen is not None:
            self._reader[CONF_PRICES], submit = self._pick(CONF_PRICES, chosen)
            return await self.async_step_prices_source(submit)
        return self.async_show_form(step_id="prices", data_schema=schema)

    async def async_step_prices_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors, placeholders, done = await self._fields(CONF_PRICES, user_input)
        if done:
            return await self.async_step_pv_forecast()
        return self.async_show_form(
            step_id="prices_source",
            data_schema=source_schema(
                CONF_PRICES, self._reader[CONF_PRICES], self.options.get(CONF_PRICES, {})
            ),
            errors=errors,
            description_placeholders={"reader": self._reader[CONF_PRICES], **placeholders},
        )

    async def async_step_pv_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._plant().has_pv:
            return await self.async_step_load_forecast()
        chosen, schema = await self._choose(CONF_PV_FORECAST, user_input, optional=True)
        if chosen == NO_SOURCE:
            self.options.pop(CONF_PV_FORECAST, None)
            return await self.async_step_load_forecast()
        if chosen is not None:
            self._reader[CONF_PV_FORECAST], submit = self._pick(CONF_PV_FORECAST, chosen)
            return await self.async_step_pv_forecast_source(submit)
        return self.async_show_form(step_id="pv_forecast", data_schema=schema)

    async def async_step_pv_forecast_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors, placeholders, done = await self._fields(CONF_PV_FORECAST, user_input)
        if done:
            return await self.async_step_load_forecast()
        return self.async_show_form(
            step_id="pv_forecast_source",
            data_schema=source_schema(
                CONF_PV_FORECAST,
                self._reader[CONF_PV_FORECAST],
                self.options.get(CONF_PV_FORECAST, {}),
            ),
            errors=errors,
            description_placeholders={"reader": self._reader[CONF_PV_FORECAST], **placeholders},
        )

    async def async_step_load_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        chosen, schema = await self._choose(CONF_LOAD_FORECAST, user_input, optional=True)
        if chosen == NO_SOURCE:
            self.options.pop(CONF_LOAD_FORECAST, None)
            return self._create()
        if chosen is not None:
            self._reader[CONF_LOAD_FORECAST], submit = self._pick(CONF_LOAD_FORECAST, chosen)
            return await self.async_step_load_forecast_source(submit)
        return self.async_show_form(step_id="load_forecast", data_schema=schema)

    async def async_step_load_forecast_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors, placeholders, done = await self._fields(CONF_LOAD_FORECAST, user_input)
        if done:
            return self._create()
        return self.async_show_form(
            step_id="load_forecast_source",
            data_schema=source_schema(
                CONF_LOAD_FORECAST,
                self._reader[CONF_LOAD_FORECAST],
                self.options.get(CONF_LOAD_FORECAST, {}),
            ),
            errors=errors,
            description_placeholders={"reader": self._reader[CONF_LOAD_FORECAST], **placeholders},
        )

    def _create(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._title, data={CONF_PLANT: self.plant_data}, options=self.options
        )


_ENGINE_FIELDS = (
    "horizon_hours",
    "slot_minutes",
    "price_max_age_minutes",
    "load_max_age_minutes",
    "pv_max_age_hours",
    "smoothing_seconds",
)


class MicrogridOptionsFlow(_Sections, OptionsFlow):
    """Edit the plant, what it measures, its sources and the engine's settings."""

    def __init__(self) -> None:
        self._reader = {}
        self._found = {}
        #: A plant edited in this flow, saved with what it measures.
        self._new_plant: dict[str, Any] | None = None

    @property
    def plant_data(self) -> dict[str, Any]:  # type: ignore[override]
        if self._new_plant is not None:
            return self._new_plant
        return dict(self.config_entry.data[CONF_PLANT])

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self.options = dict(self.config_entry.options)
        menu = ["plant", "measured", "prices"]
        if self._plant().has_pv:
            menu.append("pv_forecast")
        menu.extend(["load_forecast", "engine"])
        return self.async_show_menu(step_id="init", menu_options=menu)

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self.options)

    def _keep_plant(self, data: dict[str, Any]) -> None:
        self._new_plant = data

    async def _after_measured(self) -> ConfigFlowResult:
        # The plant is known by its grid meter: a new one is the entry's new id.
        known = plant_id(self._plant(), self.options.get(CONF_MEASURED, {}))
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_PLANT: self.plant_data},
            unique_id=known,
        )
        return self._save()

    async def async_step_plant(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        text = plant_to_toml(self.plant_data)
        if user_input is not None:
            text = user_input[CONF_PLANT_TOML]
            try:
                data = plant_from_toml(text)
                read_plant(data, self.hass.config.time_zone)
            except SetupError as err:
                errors["base"], placeholders = "invalid", _problems(err)
            else:
                # What it measures follows the plant (its meters, a battery's SOC): the
                # measured step comes next, and saves the two together.
                self._new_plant = data
                return await self.async_step_measured()
        schema = vol.Schema(
            {
                vol.Required(CONF_PLANT_TOML, default=text): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                )
            }
        )
        return self.async_show_form(
            step_id="plant",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_measured(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        plant = self._plant()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            section = measured_from_form(plant, user_input)
            try:
                check_measured(plant, section)
            except SetupError as err:
                errors["base"], placeholders = "invalid", _problems(err)
            else:
                return await self._measured_kept(plant, section)
        return self.async_show_form(
            step_id="measured",
            data_schema=measured_schema(plant, await self._measured_suggested(plant)),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _source_menu(
        self,
        name: str,
        user_input: dict[str, Any] | None,
        *,
        optional: bool,
        next_step: Callable[[dict[str, Any] | None], Awaitable[ConfigFlowResult]],
    ) -> ConfigFlowResult:
        chosen, schema = await self._choose(name, user_input, optional=optional)
        if chosen == NO_SOURCE:
            self.options.pop(name, None)
            return self._save()
        if chosen is not None:
            self._reader[name], submit = self._pick(name, chosen)
            return await next_step(submit)
        return self.async_show_form(step_id=name, data_schema=schema)

    async def _source_fields(
        self, name: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors, placeholders, done = await self._fields(name, user_input)
        if done:
            return self._save()
        return self.async_show_form(
            step_id=f"{name}_source",
            data_schema=source_schema(name, self._reader[name], self.options.get(name, {})),
            errors=errors,
            description_placeholders={"reader": self._reader[name], **placeholders},
        )

    async def async_step_prices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._source_menu(
            CONF_PRICES, user_input, optional=False, next_step=self.async_step_prices_source
        )

    async def async_step_prices_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._source_fields(CONF_PRICES, user_input)

    async def async_step_pv_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._source_menu(
            CONF_PV_FORECAST,
            user_input,
            optional=True,
            next_step=self.async_step_pv_forecast_source,
        )

    async def async_step_pv_forecast_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._source_fields(CONF_PV_FORECAST, user_input)

    async def async_step_load_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._source_menu(
            CONF_LOAD_FORECAST,
            user_input,
            optional=True,
            next_step=self.async_step_load_forecast_source,
        )

    async def async_step_load_forecast_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._source_fields(CONF_LOAD_FORECAST, user_input)

    async def async_step_engine(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        current = dict(self.options.get(CONF_ENGINE, {}))
        if user_input is not None:
            engine: dict[str, Any] = {
                key: user_input[key] for key in _ENGINE_FIELDS if user_input.get(key) is not None
            }
            if user_input.get("battery_value"):
                engine["battery_value"] = user_input["battery_value"]
            try:
                read_engine(engine, self._plant())
            except SetupError as err:
                errors["base"], placeholders = "invalid", _problems(err)
            else:
                self.options[CONF_ENGINE] = engine
                return self._save()
        schema: dict[Any, Any] = {}
        for key in _ENGINE_FIELDS:
            value = current.get(key)
            marker = (
                vol.Optional(key, description={"suggested_value": value})
                if value is not None
                else vol.Optional(key)
            )
            schema[marker] = _number()
        schema[
            vol.Optional(
                "battery_value", description={"suggested_value": current.get("battery_value")}
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["stored_cost", "replacement"], translation_key="battery_value"
            )
        )
        return self.async_show_form(
            step_id="engine",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders=placeholders,
        )
