# AGENTS.md

Guidance for people and coding agents working in this repo.

## What this is

A library that models a solar + battery plant and produces **marginal energy
prices**: what one more kWh costs, slot by slot, and the value of energy by source
(eman/tariffkit#67). That is the product: the plant model (`microgrid.plant`, usable on
its own), the price model (`microgrid.engine`), and their command, `microgrid`
(`microgrid.cli`), which runs them on a dataset or on a site's live sources. How the
prices are made is in `docs/methodology/`.

A local checkout may hold development records that are never published and that git
ignores (`docs/adr/`, `docs/research/`, `docs/INDEX.md`). Read them where they exist;
never cite them in a tracked file.

## Rules

- **The scope test.** The job is to produce marginal prices. Before anything is built,
  and in every review, the library holds only what models the plant or produces
  marginal prices: the plant model and its projection, the price model, the battery's
  lots and their cost split, the series and units inputs arrive in, and the
  model-facing checks against the meters. Each module states which it serves. Its
  command (`microgrid.cli`) runs these on a dataset (`dataset/1`), or on the live
  sources a `sources/1` file names, read by the library's readers. It holds no billing,
  earnings, control (tariffkit#67 R-15) or load forecasting (R-17).
- **Advisory only.** Nothing here controls a device (tariffkit#67 R-15).
- **Not a tariff engine** (tariffkit#67 R-16). The library takes prices as an input
  series and computes no tariff.
  **Not a load forecaster** (R-17): the load forecast is an input series.
- **Prices energy; models no load.** No load's run is priced and none is taken out of
  the house load: it is the plant's metered consumption, as is. A DC-bus load stays in
  the plant model as topology.
- **Degrade conservatively.** Missing or stale inputs move values toward the import
  price and are named in `degraded`; never reuse a stale discount (tariffkit#67 R-14).
- **The models stand alone.** The library is the repository's root project
  (`src/microgrid`):
  - the plant model (`microgrid.plant`) does no I/O and imports nothing of the price
    model or the CLI;
  - the price model (`microgrid.engine`) does no I/O and imports nothing from the CLI;
  - the CLI (`microgrid.cli`) reads files and the network and uses only the plant's and
    engine's public names.

  Clients import the library's public tiers and the CLI's declared surface (each
  module's `__all__`). No I/O in the models is the one rule; the architecture test
  (`tests/test_architecture.py`) enforces it.
- **Nothing site-specific in code.** Plant parameters live in the plant's file
  (`plant/2`), a site's sources in its `sources/1` file; secrets and hosts never in a
  tracked file.
- **Formats are versioned.** The library's formats (`plant/2`, `engine/2`,
  `dataset/1`, `sources/1`, `valuation/2`) and its API (`ENGINE_CONTRACT`) are
  versioned; a change that is not additive bumps the version, and says so in
  `CHANGELOG.md`.
- **Precedence:** code wins over the docs.

## Conventions

- Python 3.14, uv; `uv.lock` committed. ruff (100 cols), mypy `strict`.
- **The models hold UTC only** (`require_utc` in the engine):
  Python compares and subtracts aware datetimes that share a `tzinfo` by wall clock,
  which breaks across DST. Adapters convert at the edge; publish ISO 8601 with an offset.
- Money is a float in the prices' currency (never converted, and no name says one),
  rounded at serialization. Power kW, energy kWh.
- Every non-obvious number or rule cites its source, one a reader of this repository
  can open: a TariffKit requirement (`tariffkit#67 R-7`), a format's reference page, a
  methodology page, or the measurement itself, stated in place (what was measured,
  where, over what span). Never a private issue, a local record or a conversation.
- **Requirements are this repository's own:** eman/tariffkit#67 and the library's
  public names. Consumers adapt to what is published; their requirements documents are
  not this repository's spec.
- Tests: unit and property (hypothesis) tests; golden fixtures are synthetic or outputs
  of synthetic inputs (`examples/dataset`, `scripts/make_example_dataset.py`); anything
  needing a live site is marked `live`.
- `CHANGELOG.md` gets an entry under `[Unreleased]` for each user-visible change.

## CI

GitHub Actions runs `scripts/ci_local.sh` on every push to `main` and every
pull request (`.github/workflows/ci.yml`), docs included (it builds the docs site). The same
script is the pre-push hook, so run it locally first:

```sh
scripts/install-hooks.sh   # once: pre-push runs the gate
scripts/ci_local.sh        # the gate: lock, ruff, format, mypy, docs, pytest
```
