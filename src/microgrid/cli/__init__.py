"""The ``microgrid`` command (:mod:`microgrid.cli.main`): the library run on a dataset's
files (``dataset/1``), or on a site's live sources (``sources/1``: ``now``, ``sources``,
``fetch``).

- :mod:`.dataset`: the dataset format, read and written; :mod:`.inputs`, a dataset as
  the engine's inputs.
- :mod:`.history`: what a plant's meters recorded, read through a port a dataset (or a
  client's recorder) answers, for the commands that score against it and for the
  battery's lots.
- :mod:`.offline`, :mod:`.replay`, :mod:`.check_plant`, :mod:`.fit_battery`,
  :mod:`.evaluation`: the commands' work on a dataset.
- :mod:`.signs`: each meter's sign, told from what it recorded; :mod:`.seen`, a
  battery's rating as its meters show it.
- :mod:`.sources`: the readers of the live sources a ``sources/1`` file names (Home
  Assistant, TariffKit, Solcast, EMHASS, Predbat, any entity), behind the ``sources``
  extra. A service that runs the library on a site builds on them.

**The public surface** is each module's ``__all__``: what another client may import. The
library's architecture test holds every module to declaring one.

It reads and writes files and the network.
"""

__all__ = [
    "check_plant",
    "dataset",
    "evaluation",
    "fit_battery",
    "history",
    "inputs",
    "main",
    "offline",
    "replay",
    "seen",
    "signs",
    "sources",
    "toml_text",
]
