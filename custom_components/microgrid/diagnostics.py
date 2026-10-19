"""Diagnostics: the configuration (entity ids redacted), what each input rests on, the
causes and the battery's lots."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_MEASURED, SOURCE_SECTIONS
from .coordinator import MicrogridConfigEntry

#: Where an entry names a Home Assistant entity or config entry.
_REDACT = {
    "entity",
    "entities",
    "export_entities",
    "price",
    "forecast",
    "feed_in_price",
    "feed_in_forecast",
    "current_rate",
    "next_structure_time",
    "adjustment",
    "sell_rate",
    "last_polled",
    "config_entry",
    "soc",
    "house",
    "dispatch",
    "heartbeat",
    "import_price",
    "export_price",
    "inverter",
    "meters",
    "home",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MicrogridConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    snapshot = coordinator.data
    valuation = coordinator.last_valuation
    published = None if snapshot is None else snapshot.published
    lots = None if snapshot is None else snapshot.lots
    return {
        "plant": dict(entry.data.get("plant", {})),
        "engine": coordinator.setup.engine.to_mapping(),
        "sources": {
            name: async_redact_data(dict(entry.options.get(name, {})), _REDACT)
            for name in (*SOURCE_SECTIONS, CONF_MEASURED)
            if name in entry.options
        },
        "read": {} if snapshot is None else snapshot.sources,
        "problems": {} if snapshot is None else snapshot.problems,
        "last_valuation": None
        if valuation is None
        else {
            "now": valuation.now.isoformat(),
            "kind": valuation.kind.value,
            "degraded": [str(cause) for cause in valuation.degraded],
            "inputs": {
                str(name): {
                    "as_of": None if state.as_of is None else state.as_of.isoformat(),
                    "usable": state.usable,
                }
                for name, state in valuation.inputs.items()
            },
            "slots": len(valuation.slots),
        },
        "published": None
        if published is None
        else {
            "issued_at": published.valuation.now.isoformat(),
            "valid_until": published.valid_until.isoformat(),
            "reason": published.reason.value,
        },
        "live": None if snapshot is None or snapshot.live is None else snapshot.live.to_mapping(),
        "lots": None
        if lots is None
        else {
            "as_of": lots.as_of.isoformat(),
            "held": {str(s): {"kwh": a.kwh, "value": a.value} for s, a in lots.held.items()},
            "pool": None if lots.pool is None else {"kwh": lots.pool.kwh, "value": lots.pool.value},
        },
    }
