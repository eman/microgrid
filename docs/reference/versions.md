# Versions

Four versions, each saying something different:

| Version | Example | Where you see it | What it says |
|---|---|---|---|
| the release | `0.1.0` | PyPI, `microgrid --version`, the [changelog](https://github.com/eman/microgrid/blob/main/CHANGELOG.md) | which code you have |
| each format's | `plant/2`, `engine/2`, `dataset/1`, `sources/1`, `valuation/2` | a file's `schema` (a dataset's `manifest.json`: `format`), the outputs' schema (`microgrid schema`) | the shape of the data |
| the API's | `engine-api/5` | `microgrid.engine.ENGINE_CONTRACT` | the shape of the Python API |
| the model's | `pricing/1.3` | every valuation's `model_version` | how the prices are made |

`microgrid --version` prints the release with the API and the model it carries:
`microgrid 0.1.0 (engine-api/5, pricing/1.3)`.

## The release

Releases follow [semantic versioning](https://semver.org/), MAJOR.MINOR.PATCH.

- **Before 1.0**, a minor release (`0.2.0`) may change a format, the API or the model.
  Each change bumps that one's version, and the release's section of the changelog names
  it and what to do about it. A patch release (`0.1.1`) changes none of them: its
  fixes leave every format, the API and the prices for the same inputs as they were.
- **From 1.0**, only a major release changes a format or the API in a way that is not
  additive. A change to the model, which changes prices but no interface, is a minor
  release.

The API is the public modules' names (each module's `__all__`, the [API pages](index.md));
every other module is private and may change in any release.

Before 1.0, pin the minor release you tested with:

```toml
dependencies = ["microgrid ~= 0.1.0"]
```

## The other versions

- **A format's** version goes up when a change is not additive: a renamed or removed key,
  or a value read differently. An older file is either still read, as it meant
  (`engine/1`), or refused with a message saying what changed (a `plant/1` file names
  the keys `plant/2` renamed).
- **The API's** goes up when a public name is removed, renamed or changes what it
  takes or gives in a way that breaks a caller.
- **The model's** goes up when a change to the price rule or the plant model would
  change a value for the same inputs.
