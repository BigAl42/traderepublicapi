---
name: trade-republic-mcp
description: >-
  Bedient den Trade-Republic MCP-Adapter (Hermes): Portfolio lesen, Watchlist
  und echte Orders mit confirm_token. Nutzen bei Trade Republic, TR-MCP,
  Depot, Orders, Stop-Loss, Watchlist, get_adapter_status, place_limit_order
  oder mcp__trade-republic__.
---

# Trade Republic MCP — Agent-Kurzanleitung

Unoffizieller Adapter. **Kein Dry-Run** bei Mutationen. Session warm halten
offline (`check_login.py`), nicht per Push-Login aus Tool-Loops.

Ausführliche Deploy-/Ops-Doku: [tr-adapter/HERMES.md](../../../tr-adapter/HERMES.md).

## Vor jedem riskanten Schritt

1. Bei Fehlern zuerst `get_adapter_status` (lokal, kein TR-Call).
2. JSON-Fehler parsen: `code`, `retry_after_seconds`, `guidance` befolgen.
3. Nie Push-Login aus Read-Loops triggern, wenn `TR_MCP_ALLOW_INTERACTIVE_LOGIN=0`.
4. Bei `login_required` / `session_expired`: `renew_session` (App-Push bestätigen lassen),
   dann denselben Read einmal retryen — nicht auf andere Provider wechseln.
5. Bei `rate_limited` / Status `cooldown`: warten, keine TR-Calls, User informieren.
6. Bei Status `write_backoff`: keine Mutationen; State mit Reads prüfen.

## Flags (Default: aus)

| Env | Erlaubt |
|-----|---------|
| `TR_MCP_WRITE_ENABLED=1` | Watchlist add/remove |
| `TR_MCP_TRADING_ENABLED=1` | Limit / Stop-Market / Cancel (**echtes Geld**) |

`trading_disabled` / `writes_disabled` → Flag fehlt; nicht umgehen.

## Reads — bevorzugte Reihenfolge

Portfolio / Konto:

1. `get_adapter_status` (bei Fehlern / Unsicherheit)
2. `get_account_summary`
3. `list_active_positions` / `get_position_details`
4. `list_open_orders` / `list_order_history` / `get_order` / `list_savings_plans` /
   `list_price_alarms`
5. `get_recent_transactions` oder `get_full_timeline` → `get_transaction_detail`

Research / Pre-Trade:

- `search_instruments` → ISIN
- `get_live_quote` / `get_price_history` / Analysen (`get_stock_analysis`, …)
- Vor Order: `get_order_preview`, `get_instrument_suitability`, Cash via `get_account_summary`

Ticker = ISIN (z. B. `US0378331005`).

## Confirm-Token-Flow (alle Mutationen)

Gilt für Watchlist **und** Trading:

1. Tool mit `confirmed=false` (Default) aufrufen.
2. Antwort: `status=confirmation_required`, deutsche `message`, `confirm_token`.
3. `message` dem User zeigen; **nur bei klarer Zustimmung** fortfahren.
4. Denselben Call mit **identischen Parametern**, `confirmed=true` und dem Token.
5. Bloßes `confirmed=true` ohne Token → abgelehnt.
6. Parameter geändert (Size/Limit/Stop/…) → Token ungültig; neu previewen.

## Watchlist

Nur wenn User es will und Write-Flag an:

- `add_to_watchlist` / `remove_from_watchlist`
- Nach `uncertain`: warten (`retry_after_seconds`), `get_watchlist`, **nicht** sofort erneut mutieren.

## Trading (echtes Geld)

Nur wenn User **explizit** kaufen/verkaufen/stornieren will und Trading-Flag an.

| Tool | Zweck |
|------|--------|
| `place_limit_order` | Limit buy/sell |
| `place_stop_market_order` | Stop-Market; **sell = Stop-Loss** |
| `cancel_order` | Offene Order per `order_id` |

Ablauf:

1. Pre-Trade-Reads (Preview, Suitability, Cash/Position).
2. `confirmed=false` → User die deutsche Warnung zeigen (enthält „ECHTES GELD“).
3. Zustimmung → gleiche Parameter + Token ausführen.
4. Bei `uncertain`: warten, `list_open_orders`, nicht sofort erneut platzieren/canceln.
5. Stop-Loss ändern = `cancel_order` + neu `place_stop_market_order` (kein changeOrder).

Typische Defaults: Limit `expiry=gfd`, Stop-Loss `expiry=gtc`, Exchange `LSX`.

## Fehler-Codes (kurz)

| code / status | Aktion |
|---------------|--------|
| `login_required` / `session_expired` | `renew_session` (Push) oder offline `check_login.py` / `TR_TOKEN` |
| `rate_limited` / `cooldown` | Warten `retry_after_seconds` |
| `write_backoff` | Warten; Reads statt Mutates |
| `writes_disabled` | Watchlist-Flag fehlt |
| `trading_disabled` | Trading-Flag fehlt |
| `confirmation_required_or_invalid` | Neu mit `confirmed=false` previewen |

## Verbote

- Keine Orders/Watchlist-Änderungen ohne explizite User-Zustimmung.
- Kein Trading „zum Testen“ — kein Dry-Run.
- Kein Spam von Login / `check_login.py` / Mutationen bei Cooldown/Backoff.
- Hermes-Toolnamen: `mcp__<server>__<tool>` (z. B. `mcp__trade-republic__get_adapter_status`).
