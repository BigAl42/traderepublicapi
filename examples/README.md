# Examples

For these examples, copy `environment_template.py` to `environment.py` and enter your Trade Republic login details.

# ISINs

Some company names cannot be resolved automatically. To still include those ISINs in the export, add them to `companyNameIsins.json`.

# Scripts

The following scripts form a workspace to fetch and process stocks and transactions from Trade Republic.

## portfolioExporter.py

Reads the current portfolio from Trade Republic and saves it as `myPortfolio.json`.

## timelineExporter.py

Saves the complete timeline to `myTimeline.json`.

## isinDownloader.py

Queries stock details. Each ISIN is stored under `stock_details/` as a JSON file named after the ISIN.

usage: isinDownloader.py [-h] [-i ISIN] [-f FILE] [-p] [-c]

optional arguments:
-h, --help            show this help message and exit
-i ISIN, --isin ISIN  Crawl single ISIN
-f FILE, --file FILE  Crawl a list of ISINs
-p, --portfolio       Crawl all stocks from myPortfolio.json
-c, --combine         Combine all stock data to a single JSON file

```bash
python3 isinDownloader.py -i US72919P2020
python3 isinDownloader.py -f isins.txt
```

If the portfolio file has already been downloaded, query all stocks in the portfolio with:

```bash
python3 isinDownloader.py -p
```

The following command creates a single `allStocks.json` file that combines all downloaded ISINs.

```bash
python3 isinDownloader.py -c
```

## timelineCsvConverter.py

Converts the timeline to CSV. For this script to work correctly, all traded stocks must have been downloaded with the ISIN downloader.

**Warning:** Some stocks have different names at Lang & Schwarz than in the Trade Republic app. Trade Republic also uses inconsistent names itself. The ISIN may not be assigned automatically for every stock. In that case an error is printed and the ISIN must be copied into the CSV file manually.

The export is optimized for Portfolio Performance. The following transactions are currently processed:

- Withdrawal
- Deposit
- Buy
- Savings plan execution
- Sell
- Dividend

# Export for Portfolio Performance

*TODO:* Reinvested dividends cannot be exported yet.

The following scripts produce a CSV file for Portfolio Performance.

```bash
python3 portfolioExporter.py
python3 timelineExporter.py
python3 isinDownloader.py -p -c
python3 timelineCsvConverter.py
```

If `WARNING: Company not found` appears while creating the CSV, add the missing ISINs to `companyNameIsins.json`. Running the converter again should then produce a complete export.

```bash
python3 timelineCsvConverter.py
```
