# Trade Republic API

Unofficial Python client for the Trade Republic private API. Not affiliated with Trade Republic Bank GmbH.

Use it to read cash/portfolio, search instruments, stream quotes, export the timeline, or experiment with orders. **Pairing a device logs the official app out** until you pair the phone again.

Capability matrix, known gaps, and how to test: [docs/API.md](docs/API.md).

## Install

```bash
python3 -m pip install -r trapi/requirements.txt
```

Device key path defaults to `./key` (gitignored). Override with `TrBlockingApi(..., key_file=...)` or `TR_KEY_FILE`.

```bash
make check   # syntax
make test    # offline unit tests, no account required
```

## Timeline CSV export

Tested on Linux, Python 3.8, German locale.

- Update `./examples/envConsts.py` with output paths.
- Copy `examples/environment_template.py` to `examples/environment.py` and set your TR account.
- See `startMe.sh` for the download → details/PDFs → CSV flow.

More example scripts: [examples/README.md](examples/README.md).

## Example: blocking timeline

```python3
from trapi.api import TrBlockingApi

def main():
    tr = TrBlockingApi(NUMBER, PIN)
    tr.login()

    res = tr.timeline()
    print(res.keys())
    for x in res["data"]:
        print(tr.timeline_detail(x["data"]["id"]))
```

## Example: async quotes

```python3
import asyncio
from trapi.api import TRApi

def process(json_data):
    print("I am a processor: ", json_data)

async def main():
    tr = TRApi(NUMBER, PIN)
    tr.login()

    await tr.cash(callback=lambda x: print(f"Cash data: {x}"))
    await tr.portfolio()

    isin = "US62914V1061"
    await tr.instrument(isin)
    await tr.stock_details(isin)
    await tr.ticker(isin, callback=process)
    await tr.neon_news(isin)

    await tr.start()

if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(main())
```

# JSON Format

Sample payloads from the Trade Republic timeline API (German locale). JSON `title`/`body` strings are broker data, not project documentation.

## Dividend (`Dividende`)
```json
{
	"type": "timelineEvent",
	"data": {
		"id": "1512453d-1880-4b46-ac4e-2a8ee3f97187",
		"timestamp": 1616811300786,
		"icon": "https://assets.traderepublic.com/img/icon/timeline/Dividend.png",
		"title": "Stock XYZ",
		"body": "Gutschrift Dividende pro Aktie von 0,40 USD",
		"cashChangeAmount": 1.97,
		"action": {
			"type": "timelineDetail",
			"payload": "1512453d-1880-4b46-ac4e-2a8ee3f97187"
		},
		"attributes": [

		],
		"month": "2021-03"
	}
}
```

## Cash in (`Einzahlung`)
```json
{
	"type": "timelineEvent",
	"data": {
		"id": "7f854148-4278-45f3-8c99-e2f7059ab70c",
		"timestamp": 1616660487759,
		"icon": "https://assets.traderepublic.com/img/icon/timeline/CashIn.png",
		"title": "Einzahlung",
		"body": "Geldeingang vom Konto\nDE32120300001032514893",
		"cashChangeAmount": 100.0,
		"attributes": [

		],
		"month": "2021-03"
	}
}
```

## Cash out (`Auszahlung`)
```json
{
	"type": "timelineEvent",
	"data": {
		"id": "f4d62473-d4ed-485a-b56e-7c0509c04701",
		"timestamp": 1617126782673,
		"icon": "https://assets.traderepublic.com/img/icon/timeline/CashOut.png",
		"title": "Auszahlung",
		"body": "Geldausgang an Dein\nReferenzkonto",
		"cashChangeAmount": -5.0,
		"attributes": [

		],
		"month": "2021-03"
	}
}
```

## Savings plan execution (`Sparplan Ausführung`)
```json
{
	"type": "timelineEvent",
	"data": {
		"id": "91a39f02-376b-4fd7-a3c4-05a3cd1e52ba",
		"timestamp": 1615910518967,
		"icon": "https://assets.traderepublic.com/img/icon/timeline/SavingsPlanExecuted.png",
		"title": "Stock XYZ",
		"body": "Sparplan ausgef\u00fchrt zu 156,86 \u20ac",
		"cashChangeAmount": -9.99,
		"action": {
			"type": "timelineDetail",
			"payload": "91a39f02-376b-4fd7-a3c4-05a3cd1e52ba"
		},
		"attributes": [

		],
		"month": "2021-03"
	}
}
```

## Buy (`Kauf`)
```json
{
	"type": "timelineEvent",
	"data": {
		"id": "67ce42be-ec6a-4e97-bb1e-e4eac899bb4f",
		"timestamp": 1616690513004,
		"icon": "https://assets.traderepublic.com/img/icon/timeline/Arrow-Right.png",
		"title": "Stock XYZ",
		"body": "Kauf zu 50,99 \u20ac",
		"cashChangeAmount": -51.99,
		"action": {
			"type": "timelineDetail",
			"payload": "67ce42be-ec6a-4e97-bb1e-e4eac899bb4f"
		},
		"attributes": [

		],
		"month": "2021-03"
	}
}
```

## Sell (`Verkauf`)
```json
{
	"type": "timelineEvent",
	"data": {
		"id": "3265a78b-4738-419a-88a5-f8d3f5cc914d",
		"timestamp": 1617008391425,
		"icon": "https://assets.traderepublic.com/img/icon/timeline/Arrow-Left.png",
		"title": "Stock XYZ",
		"body": "Limit Verkauf zu 265,30 \u20ac\nRendite: \ufffc 22,20 %",
		"cashChangeAmount": 123.4,
		"action": {
			"type": "timelineDetail",
			"payload": "3265a78b-4738-419a-88a5-f8d3f5cc914d"
		},
		"attributes": [
			{
				"location": 35,
				"length": 9,
				"type": "positiveChange"
			}
		],
		"month": "2021-03"
	}
}
```
