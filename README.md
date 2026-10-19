# microgrid

`microgrid` is a library that models a solar + battery plant (`microgrid.plant`) and
produces **marginal energy prices** from it (`microgrid.engine`): what one more kWh
costs, slot by slot over a horizon, and the value of energy by source.

You describe the plant (`plant/2`: PV of either coupling, a battery on its own inverter
or on a hybrid's DC bus, loads on either bus, the grid's limits and charge windows) and
give it its inputs as time series (prices, PV and load forecasts, the meters); it returns
prices, regimes, certainties and surplus for each slot.

The documentation is in [`docs/`](https://github.com/eman/microgrid/blob/main/docs/index.md),
published with each release at `https://eman.github.io/microgrid/`:
how the prices are made, the concepts, how-to guides and the reference.

## Install

```sh
pip install microgrid                # or: uv add microgrid
pip install "microgrid[sources]"     # with the readers of live sources (Home Assistant)
```

In Home Assistant, the integration (`custom_components/microgrid`, through HACS) installs
it and runs it on Home Assistant's own entities
([Run it in Home Assistant](https://github.com/eman/microgrid/blob/main/docs/how-to/home-assistant.md)).

Python 3.14 or later. Before 1.0 a minor release may change a format, the API or the
model: pin the minor release you tested with (`microgrid ~= 0.1.0`,
[Versions](https://github.com/eman/microgrid/blob/main/docs/reference/versions.md)).

## The command

`microgrid` (`microgrid.cli`, installed with the library) runs it on a dataset
(`dataset/1`: a directory of series and the plant's and engine's files), so it needs no
plant at hand. The example
dataset (`examples/dataset`) is one synthetic week of the library's example plant in
Europe/Berlin, the DST change on 2026-10-25 inside it:

```sh
uv run microgrid value --data examples/dataset --at 2026-10-22T13:00   # one valuation
uv run microgrid live --data examples/dataset --from 2026-10-22T06:00 --to 2026-10-22T20:00
uv run microgrid replay --data examples/dataset --from 2026-10-22 --to 2026-10-25
                                        # valuations scored against the meters
uv run microgrid check-plant --data examples/dataset --from 2026-10-21 --to 2026-10-27
                                        # the plant model against its meters
uv run microgrid ledger --data examples/dataset --from 2026-10-21 --to 2026-10-27
                                        # the battery's lots and their cost split
uv run microgrid fit-battery --data examples/dataset --end 2026-10-28 --days 7
                                        # the battery's conversion, fitted
uv run microgrid config check --data examples/dataset   # its plant/2 and engine/2
uv run microgrid schema                                 # the outputs' schema
```

With a `sources/1` file naming a provider per input (the `sources`
extra, `microgrid[sources]`), it reads live inputs instead of a dataset:

```sh
uv run microgrid sources --sources sources.toml   # what each input is read from, or why not
uv run microgrid now --sources sources.toml       # a valuation now; --save DIR keeps the inputs
uv run microgrid fetch --sources sources.toml --from 2026-10-01 --to 2026-10-08 --out DIR
                                        # what was measured, as a dataset
```

A time without an offset is the plant's local time (an hour that repeats, the first
time; one the clocks skip is refused).

## Development

Python 3.14, uv. See [AGENTS.md](https://github.com/eman/microgrid/blob/main/AGENTS.md) for the rules and conventions.

```sh
uv sync                      # .venv with the library and the dev tools
scripts/install-hooks.sh     # pre-push runs the local gate
scripts/ci_local.sh          # ruff, format, mypy --strict, the docs, pytest + coverage
```

## License

MIT: see [LICENSE](https://github.com/eman/microgrid/blob/main/LICENSE).
