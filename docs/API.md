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

1. **REST** `https://api.traderepublic.com` — **web login v2** (`/api/v2/auth/web/login`) by default
2. **WebSocket** `wss://api.traderepublic.com` — `connect 31` (web trading) then `sub <id> {json}`

Subscription replies use states `A` (snapshot), `D` (delta via `decode_updates`), `C` (complete), `E` (error).

Default `auth="web"` matches `app.traderepublic.com`: phone + PIN, then confirm the push in the mobile app (or an authenticator code). The phone app **stays logged in**. Session cookies are saved to `tr_cookies.txt` (`TR_COOKIES_FILE`).

Legacy `auth="device"` is the old ECDSA `/api/v1/auth/login` path (`connect 21`). Trade Republic currently answers `CLIENT_VERSION_OUTDATED` / `failed 34` on that path.

Set `TR_APP_VERSION` if login starts failing with a version error. Current default is `2.2631.13` (`web-pro`).

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

Compared with [pytr](https://github.com/pytr-org/pytr):

- No AWS WAF / Playwright path for **v1 web login**. This client uses **v2** (no WAF).
- REST cost transparency and payout confirm.
- Order extras: `confirmOrder`, `changeOrder`, `collection`.
- Watchlist extras: named/follow/unfollow lists.
- CSV export does not cover interest, card payments, saveback, tax refunds, or reinvested dividends. Converters still expect German timeline text.
- `asyncio.get_event_loop()` in `TrBlockingApi` is the old pattern (works, noisy on Python 3.10+).

Treat trading methods as experimental. There is no dry-run.

## How to test

### Offline (no Trade Republic account)

```bash
python3 -m pip install -r trapi/requirements.txt
make check
make test
```

These cover protocol deltas, argument validation, public method surface, v2 header shape, and an unauthenticated `connect 31` search-tags call.

### Live read-only (real account)

Needed:

1. Phone number and PIN (`TR_PHONE`, `TR_PIN`).
2. Confirm the **push notification** in the Trade Republic app (or `TR_AUTHENTICATOR_CODE` if the account uses an authenticator).
3. Locale if you rely on German timeline strings (`TR_LOCALE=de`).
4. Opt-in: `TR_LIVE_TESTS=1`.

The phone app stays logged in. Cookies go to `tr_cookies.txt`.

```bash
export TR_LIVE_TESTS=1 TR_PHONE='+49...' TR_PIN='...' TR_LOCALE=de
python3 -m unittest tests.test_api.LiveReadOnlyTest -v
```

Do **not** point live tests at `simple_create_order` / cancel / savings-plan mutate methods.

Do not commit `tr_cookies.txt`, `key`, `environment.py`, or timeline dumps.
