"""Constants for the microgrid integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "microgrid"

#: The config entry's sections: the plant (``plant/2``), the engine (``engine/2``), and
#: each input's source as its ``sources/1`` section.
CONF_PLANT: Final = "plant"
CONF_ENGINE: Final = "engine"
CONF_PRICES: Final = "prices"
CONF_PV_FORECAST: Final = "pv_forecast"
CONF_LOAD_FORECAST: Final = "load_forecast"
CONF_MEASURED: Final = "measured"
SOURCE_SECTIONS: Final = (CONF_PRICES, CONF_PV_FORECAST, CONF_LOAD_FORECAST)

#: How often the forecasts are read again: each read is one issue received then.
FORECAST_READ: Final = timedelta(minutes=5)
#: How often whether to publish a valuation is decided.
DECIDE_EVERY: Final = timedelta(minutes=1)
#: How often the live value is run.
LIVE_EVERY: Final = timedelta(seconds=30)
#: Load forecast issues kept: the engine takes each slot from the newest fresh one.
LOAD_ISSUES: Final = 12
#: A source failing this long, every read since, raises a repair issue.
FAILING_FOR_ISSUE: Final = timedelta(minutes=30)

SERVICE_GET_VALUATION: Final = "get_valuation"
ATTR_CONFIG_ENTRY: Final = "config_entry"
ATTR_SLOTS: Final = "slots"
