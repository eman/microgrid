# Concepts

- **[Slots and the horizon](slots-horizon.md).** The engine prices time in slots
  (10 minutes by default) over a horizon (48 hours by default), in UTC.
- **[Series and forecasts](series.md).** Every input other than the plant's setup is a
  time series: a value holds until the next sample, and a forecast input is the series
  of its issues. The engine reads each as it stood at a moment.
- **[Inputs](inputs.md).** Each input's meaning, its name on every surface, and which
  command needs which.
- **[Units](units.md).** A series carries its unit; the engine works in kW, kWh, a
  fraction for the SOC, and the prices' currency per kWh, whatever that currency is.
- **[The plant model](plant-model.md).** How the plant behaves, behind a port: it
  projects the flows, and the price rule reads them.
- **[Outputs](outputs.md).** What a valuation gives per slot: the marginal cost, the
  regime, the surplus, the certainties, the values by source and the supply stack.

Terms of the trade (SOC, DC bus, hybrid inverter, ...) are in the [glossary](../glossary.md).
