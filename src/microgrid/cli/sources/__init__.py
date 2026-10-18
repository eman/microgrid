"""Where a valuation's inputs come from: each input from one of several optional sources,
none assumed (``sources/1``).

A ``sources.toml`` names the plant's and engine's files and, for each input, the source
it is read from: prices (``tariffkit``, ``entity``), the PV forecast (``solcast``,
``entity``), the load forecast (``emhass``, ``predbat``, ``entity``) and what is
measured (``home_assistant``). Entity names are
configuration, never defaults. A connection's token comes from the environment, never
from the file.

- :mod:`.config`: the file read, every problem named at once.
- :mod:`.home_assistant`: Home Assistant's REST API (states, services, history).
- :mod:`.tariffkit`, :mod:`.solcast`, :mod:`.emhass`, :mod:`.predbat`: the named tools'
  payloads decoded as the library's series; :mod:`.entity`, any entity whose attribute
  holds a forecast.
- :mod:`.read`: the inputs now, as the engine's ``Inputs``; :mod:`.fetch`, what was
  measured over a span, as a dataset.

These need the ``sources`` extra (``httpx``, ``pydantic``); the engine needs nothing.
They are the library's, so its command reads the sources a site configures, and a
service that runs the library on a site builds on them.
"""

__all__ = [
    "SourceError",
    "config",
    "emhass",
    "entity",
    "fetch",
    "home_assistant",
    "predbat",
    "read",
    "solcast",
    "tariffkit",
]


class SourceError(RuntimeError):
    """A source could not be read, or what it gave cannot be used: the message names it."""
