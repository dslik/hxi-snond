## SNON daemons for Sensors

This is a work-in-progress repo that contains a collection of linux daemons that read sensor values from various pieces of equipment I have in the lab.

Supported devices includes:

- HXI 1000i Power Supplies - `hxi-snond` reads per-rail voltage, amperage, and wattage, plus total wattage.
- GPM 8310 Power Analyzer - `gpm8310-snond` reads cumulative power over the measurement duration.

Sensor values are stored in SenML format, with SNON ([https://www.snon.org/](https://www.snon.org/)) used to describe the device and sensor topology:

```
{
	"eID":"urn:uuid:e82afaaa-d346-4610-9a7d-283454e4d535",
	"eC":"device",
	"eN": { "*":"HX1000i Power Supply" }
}

{
	"eID":"urn:uuid:06aa4bb6-d6ac-456d-bce6-582e1a69f514",
	"eC":"sensor",
	"eN":{ "*":"Total Output Watts" },
	"eR":{ "child_of":["urn:uuid:e82afaaa-d346-4610-9a7d-283454e4d535"] }
}
```

Some temporary Python-based scrips are included to sum time series and downsample time series to second-aligned intervals.