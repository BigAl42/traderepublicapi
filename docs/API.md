# API surface

Unofficial Trade Republic client. Not affiliated with Trade Republic Bank GmbH.
Trade Republic can change private endpoints without notice.

## Layout

```
trapi/api.py          TRApi (async WebSocket) and TrBlockingApi (sync wrappers)
trapi/__init__.py     public exports
examples/             exporters and Portfolio Performance CSV conversion
LS/                   Lang & Schwarz ISIN helper data
tests/                offline unit tests; optional live tests
docs/API.md           this file
```

`TRApi` talks to two channels:

1. **REST** `https://api.traderepublic.com` — device pairing and signed login (`/api/v1/auth/...`)
2. **WebSocket** `wss://api.traderepublic.com` — `connect 21 {locale}` then `sub <id> {json}`

Subscription replies use states `A` (snapshot), `D` (delta via `decode_updates`), `C` (complete), `E` (error).

Auth uses a P-256 device key stored as PEM. Default path is `key` in the working directory, override with `key_file=` or `TR_KEY_FILE`. Trade Republic allows **one paired device**. Pairing this library logs the mobile app out until you pair the phone again.

## What works today

### Account and portfolio (login)

| Method | TR topic | Notes |
|--------|----------|--------|
| `cash` | `cash` | cash balances |
| `available_cash` | `availableCash` | cash usable for orders |
| `available_cash_for_payout` | `availableCashForPayout` | |
| `portfolio` | `portfolio` | full portfolio |
| `compact_portfolio` | `compactPortfolio` | **legacy**; TR web may reject this since 2026 |
| `compact_portfolio_by_type` | `compactPortfolioByType` | current web portfolio; pass `secAccNo` when empty |
| `account_pairs` | `accountPairs` | securities/cash account numbers, tax wrappers |
| `portfolio_status` | `portfolioStatus` | |
| `portfolio_aggregate_history` | `portfolioAggregateHistory` | ranges: 1d, 5d, 1m, 3m, 1y, max |
| `portfolio_aggregate_history_light` | `portfolioAggregateHistoryLight` | |
| `settings` | `settings` | account settings |

### Instruments and market data

| Method | TR topic | Login |
|--------|----------|--------|
| `instrument` | `instrument` | often not required |
| `stock_details` / `stock_detail_dividends` / `stock_detail_kpis` | `stockDetails*` | login |
| `crypto_details` / `etf_details` / `etf_composition` | matching topics | login |
| `ticker` | `ticker` | quote stream `ISIN.EXCHANGE` |
| `performance` | `performance` | |
| `aggregate_history_light` | `aggregateHistoryLight` | often not required |
| `neon_search*` / `neon_news` | search/news | search often not required |
| `derivatives` | `derivatives` | categories: vanillaWarrant, knockOutProduct, factor |
| `instrument_exchange` / `home_instrument_exchange` / `instrument_suitability` | matching | |

Exchanges known to this client: `LSX`, `TDG`, `LUS`, `TUB`, `BHS`, `B2C`.

### Timeline and documents

| Method | TR topic |
|--------|----------|
| `timeline` | `timeline` |
| `timeline_detail` | `timelineDetail` |
| `timeline_detail_v2` | `timelineDetailV2` (current app payload) |
| `timeline_transactions` | `timelineTransactions` |
| `timeline_activity_log` | `timelineActivityLog` |
| `timeline_actions` | `timelineActions` |

Example scripts download PDFs from timeline detail `documents` sections (`examples/timelineExporterWithDocsAndDetails.py`).

### Watchlist, orders, savings plans, alarms (mutating where noted)

| Method | Mutating |
|--------|----------|
| `watchlist`, `add_to_watchlist`, `remove_from_watchlist` | add/remove yes |
| `orders`, `simple_create_order`, `cancel_order` | create/cancel **yes — real money** |
| `price_for_order`, `available_size` | no |
| `savings_plans`, `savings_plan_parameters` | no |
| `create_savings_plan`, `change_savings_plan`, `cancel_savings_plan` | **yes** |
| `price_alarms`, `create_price_alarm`, `cancel_price_alarm` | create/cancel yes |
| `news_subscriptions`, `subscribe_news`, `unsubscribe_news` | subscribe yes |

`TrBlockingApi` wraps the common **read** calls for scripts. Async `TRApi` plus `start()` is required for streaming (`ticker`).

## What is missing or incomplete

Compared with the current Trade Republic app / [pytr](https://github.com/pytr-org/pytr):

- **Web login v2** (push confirm / authenticator) and **AWS WAF** tokens used by `app.traderepublic.com`. This client only implements signed **v1 device-reset** login.
- **`connect 21` vs web `connect 31`**. Some topics (notably `compactPortfolio`) now fail on web sessions; use `compact_portfolio_by_type` with `secAccNo`.
- REST helpers that pytr has: signed `/api/v2/auth/account`, cost transparency, payout confirm.
- Order flow extras: `confirmOrder`, `changeOrder`, `collection`, `accruedInterestTermsRequired`.
- Watchlist extras: named/follow/unfollow/investable lists.
- CSV export does not cover interest, card payments, saveback, tax refunds, or reinvested dividends. `timelineDetailV2` is not used by the converters yet.
- First login is interactive (`input()` for SMS/app code) and historically flaky on the first attempt.
- No token refresh helper; session expiry means `login()` again.
- `asyncio.get_event_loop()` in `TrBlockingApi` is the old pattern (works, noisy on Python 3.10+).

Treat trading methods as experimental. There is no dry-run or order confirmation UI.

## How to test

### Offline (no Trade Republic account)

```bash
python3 -m pip install -r trapi/requirements.txt
make check
make test
```

These cover protocol deltas (`decode_updates`), argument validation, and the public method surface. They never call Trade Republic.

### Live read-only (real account)

Needed:

1. Phone number and PIN (`TR_PHONE`, `TR_PIN`).
2. A paired device PEM key (`TR_KEY_FILE`, default `key`). First run prints a process id and asks for the 4-digit code; **this unpairs the phone app**.
3. Locale if you rely on German timeline strings (`TR_LOCALE=de`). Several example converters only parse DE event text.
4. Opt-in flag so live tests cannot run by accident: `TR_LIVE_TESTS=1`.

```bash
export TR_LIVE_TESTS=1 TR_PHONE='+49...' TR_PIN='...' TR_LOCALE=de TR_KEY_FILE=./key
python3 -m unittest tests.test_api.LiveReadOnlyTest -v
```

Do **not** point live tests at `simple_create_order` / cancel / savings-plan mutate methods.

Cloud Agent / CI: store `TR_PHONE`, `TR_PIN`, and the device key as secrets. Expect 2FA on new environments. Do not commit `key`, `environment.py`, or timeline dumps (already gitignored).

### Manual exporters

See `examples/README.md` and `startMe.sh`. Typical path: login → timeline JSON → details/PDFs → CSV for Portfolio Performance.
