"""Where a valuation's inputs come from: each input from one of several optional sources,
none assumed (``sources/1``).

A ``sources.toml`` names the plant's and engine's files and, for each input, the source
it is read from: prices (``tariffkit``, ``nordpool``, ``amber``, ``tibber``,
``octopus``, ``entsoe``, ``openei``, ``entity``, ``action``), the PV forecast
(``solcast``, ``entity``), the load forecast (``emhass``, ``predbat``, ``entity``) and
what is measured (``home_assistant``). Entity names are configuration, never defaults.
A connection's token comes from the environment, never from the file.

- :mod:`.config`: the file read, every problem named at once.
- :mod:`.api`: what a reader asks of Home Assistant (states, actions, config entries),
  which :mod:`.home_assistant`'s REST client answers, as can a client running inside
  Home Assistant.
- :mod:`.home_assistant`: Home Assistant's REST API (states, services, history).
- :mod:`.tariffkit`, :mod:`.nordpool`, :mod:`.amber`, :mod:`.tibber`, :mod:`.octopus`,
  :mod:`.entsoe`, :mod:`.openei`, :mod:`.solcast`, :mod:`.emhass`, :mod:`.predbat`: the
  named tools' payloads decoded as the library's series; :mod:`.entity`, any entity
  whose attribute holds a forecast; :mod:`.action`, any action whose response does.
- :mod:`.rows`, :mod:`.prices`: what those readers share (rows as intervals; import
  and export prices joined).
- :mod:`.read`: the inputs now, as the engine's ``Inputs``; :mod:`.fetch`, what was
  measured over a span, as a dataset.

These need the ``sources`` extra (``httpx``, ``pydantic``); the engine needs nothing.
They are the library's, so its command reads the sources a site configures, and a
service that runs the library on a site builds on them.
"""

__all__ = [
    "SourceError",
    "action",
    "amber",
    "api",
    "config",
    "emhass",
    "entity",
    "entsoe",
    "fetch",
    "home_assistant",
    "nordpool",
    "octopus",
    "openei",
    "predbat",
    "prices",
    "read",
    "rows",
    "solcast",
    "tariffkit",
    "tibber",
]


class SourceError(RuntimeError):
    """A source could not be read, or what it gave cannot be used: the message names it."""
