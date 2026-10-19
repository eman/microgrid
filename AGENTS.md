# AGENTS.md

Guidance for people and coding agents working in this repo.

## What this is

A library that models a solar + battery plant and produces **marginal energy
prices**: what one more kWh costs, slot by slot, and the value of energy by source
(`docs/methodology/requirements.md`). That is the product: the plant model (`microgrid.plant`, usable on
its own), the price model (`microgrid.engine`), and their command, `microgrid`
(`microgrid.cli`), which runs them on a dataset or on a site's live sources. A Home
Assistant integration (`custom_components/microgrid`) runs them inside Home Assistant.
How the prices are made is in `docs/methodology/`.

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
  sources a `sources/1` file names, read by the library's readers; the Home Assistant
  integration runs them inside Home Assistant on the live sources it is configured
  with, read by the same readers. None of it holds billing, earnings, control or load
  forecasting (R-9).
- **No control.** Nothing here controls a device.
- **Not a tariff engine** (R-8). The library takes prices as an input series and
  computes no tariff.
  **Not a load forecaster** (R-9): the load forecast is an input series.
- **Prices energy; models no load.** No load's run is priced and none is taken out of
  the house load: it is the plant's metered consumption, as is. A DC-bus load stays in
  the plant model as topology.
- **Degrade conservatively.** Missing or stale inputs move values toward the import
  price and are named in `degraded`; never reuse a stale discount (R-7).
- **The models stand alone.** The library is the repository's root project
  (`src/microgrid`):
  - the plant model (`microgrid.plant`) does no I/O and imports nothing of the price
    model or the CLI;
  - the price model (`microgrid.engine`) does no I/O and imports nothing from the CLI;
  - the CLI (`microgrid.cli`) reads files and the network and uses only the plant's and
    engine's public names.

  Clients, the Home Assistant integration among them, import the library's public
  tiers and the CLI's declared surface (each module's `__all__`). No I/O in the models is the one rule; the architecture test
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
  can open: a requirement (`R-7`, in `docs/methodology/requirements.md`), a format's
  reference page, a methodology page, or the measurement itself, stated in place (what
  was measured, where, over what span). Never a private issue, a local record or a
  conversation.
- **Requirements are this repository's own:** `docs/methodology/requirements.md` and
  the library's public names. Consumers adapt to what is published; their requirements
  documents are not this repository's spec.
- Tests: unit and property (hypothesis) tests; golden fixtures are synthetic or outputs
  of synthetic inputs (`examples/dataset`, `scripts/make_example_dataset.py`); anything
  needing a live site is marked `live`.
- `CHANGELOG.md` gets an entry under `[Unreleased]` for each user-visible change.

## The Home Assistant integration

`custom_components/microgrid` beside `src/`, as one repository holds both (HACS installs
it from a release's `microgrid.zip`). Home Assistant's conventions hold in that
directory; its tests (`tests/ha`) run in the gate on Home Assistant's own test harness.
Its manifest pins this version of the library (`microgrid[sources]==X.Y.Z`), so the
integration installs only once that version is on PyPI; `tests/test_manifest.py` holds
the manifest to `pyproject.toml`'s version.

## CI

GitHub Actions runs `scripts/ci_local.sh` on every push to `main` and every
pull request (`.github/workflows/ci.yml`), docs included (it builds the docs site). The same
script is the pre-push hook, so run it locally first:

```sh
scripts/install-hooks.sh   # once: pre-push runs the gate
scripts/ci_local.sh        # the gate: lock, ruff, format, mypy, docs, pytest
```

## Releases

Versions follow `docs/reference/versions.md`: semantic versioning, the version in
`pyproject.toml` alone (`microgrid --version` reads it). Between releases it is the next
release's, with `.dev0`. To release `X.Y.Z`:

1. In one change on `main`: `CHANGELOG.md`'s `[Unreleased]` becomes `[X.Y.Z] - YYYY-MM-DD`
   (a new empty `[Unreleased]` above it), `pyproject.toml`'s version becomes `X.Y.Z`
   (`uv version X.Y.Z`, which updates `uv.lock`), and so do the integration's manifest's
   `version` and requirement (`custom_components/microgrid/manifest.json`). The gate
   passes.
2. Tag that commit `vX.Y.Z` and push the tag. `.github/workflows/release.yml` checks the
   tag is the version, runs the gate, builds, publishes to PyPI by trusted publishing
   (the `pypi` environment), makes the GitHub release, its notes the changelog's
   section, with the integration's `microgrid.zip` for HACS, and publishes the
   documentation site to GitHub Pages (the `github-pages` environment): one site, the
   latest release's.
3. Then `pyproject.toml`'s version, and the manifest's, become the next one's `.dev0`
   (`uv version X.Y.(Z+1).dev0`).

A version on PyPI is never replaced: a mistake is fixed by the next release.

