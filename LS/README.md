# Lang & Schwarz

The Lang & Schwarz trading universe is available at:
https://www.ls-x.de/de/handelsuniversum

# PDF conversion

The trading universe is provided as a PDF. Convert it to JSON and CSV with Tabula. The script `convert-stammdaten.py` produces a JSON array with the main fields: WKN, ISIN, Name, Symbol.

```json
[
	[
		"554550",
		"DE0005545503",
		"1+1 DRILLISCH AG O.N.",
		"DRI"
	],
    ...
	[
		"A0LEPS",
		"FR0010285965",
		"1000MERCIS INH.EO-,10",
		"XXX"
	]
]
```
