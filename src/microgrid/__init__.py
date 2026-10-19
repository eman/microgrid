"""microgrid: a solar + battery plant modelled, and its marginal energy prices.

- :mod:`microgrid.plant`: the plant model and its simulator, on their own: a plant
  described as data (``plant/2``), projected slot by slot.
- :mod:`microgrid.engine`: the marginal prices, a client of the plant model.
- :mod:`microgrid.cli`: the ``microgrid`` command, the library run on a dataset's files
  (``dataset/1``) or on a site's live sources (``sources/1``, the ``sources`` extra). A
  client of the public names, as any other.

A requirement cited as R-n is the documentation's (``docs/methodology/requirements.md``).
"""
