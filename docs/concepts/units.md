# Units

The engine works in **kW, kWh, a fraction for the SOC (0.62, not 62), and the prices'
currency per kWh**. A source gives what it gives: watts or kilowatts, a percentage, a
price per MWh. So every series carries its unit (`Series.unit`), and
`microgrid.engine.units` converts it once, at the edge:

| Quantity | Units it knows | Becomes |
|---|---|---|
| `power` | `W`, `kW`, `MW` | kW |
| `energy` | `Wh`, `kWh`, `MWh` | kWh |
| `fraction` | `%`, or no unit | a fraction (no unit means it already is one) |
| `price` | `<currency>/Wh`, `<currency>/kWh`, `<currency>/MWh` | the same currency per kWh |

```python
from microgrid.engine import units

units.convert(42.0, "%", "fraction")          # 0.42
units.convert(85.0, "EUR/MWh", "price")       # 0.085, EUR per kWh
```

`units.normalise(series, quantity)` converts a whole series. A dataset's units are
converted when it is read; a Python client converts its own inputs before handing them
over (the engine reads the numbers as given).

**A unit it does not know is refused** (`UnitError`): a reading in an unknown unit is
not used.

## Currency

**Prices may be in any currency, and the library never converts one.** Every money
figure out of the library is in the prices' currency. `EUR/MWh` becomes `EUR/kWh`;
`¢/kWh` passes through as a currency named `¢`, a hundredth of what a dollar price would
say.

No name says a currency: `per_kwh` in `valuation/2`, the battery's wear
(`wear_charge_per_kwh`, `wear_discharge_per_kwh`) in `plant/2`. Give the wear in the
prices' currency too. The command's tables head their money columns with the dataset's
currency (`cost EUR`, `EUR/kWh`), and a dataset whose prices name two currencies is
refused, never mixed. A recorded price in a per-MWh unit is converted like a forecast's.

## Signs

Each meter's sign convention is in `plant/2` (`meters[].sign`), the one place it is
stated.
