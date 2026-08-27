---
name: trade-republic-mcp
description: >-
  Bedient den Trade-Republic MCP-Adapter (Hermes): Session läuft unsichtbar in
  Read/Write-Tools, Portfolio lesen, Watchlist und echte Orders mit
  confirm_token. Nutzen bei Trade Republic, TR-MCP, login_required,
  session_expired, awaiting_push_confirm, Depot, Orders, Stop-Loss, Watchlist,
  get_adapter_status oder mcp__trade-republic__.
---

# Trade Republic MCP — Agent-Kurzanleitung

Unoffizieller Adapter. **Nur MCP-Tools** für Hermes — kein selbstgeschriebenes
Python, keine Shell-Login-Skripte. Mutationen: **kein Dry-Run**.

Deploy/Ops: [tr-adapter/HERMES.md](../../../tr-adapter/HERMES.md)
(`check_login.py` ist **Operator-only**, nicht für den Agenten).

## Harte Verbote (Session)

- **Nie** `trigger_login.py`, `check_login.py`, `tr.login()`, `start_web_login` oder
  anderes Custom-Python ausführen — das startet **jedes Mal einen neuen Push**.
- **Nie** einen zweiten Login starten, solange `awaiting_push_confirm` /
  `login_process_id` gesetzt ist.
- **Kein** separates Login-Tool — Session erneuert der Adapter automatisch in
  den normalen Read/Write-Tools.

## Session / Push-Login (Pflichtprotokoll)

Bei `login_required` / `session_expired` / `awaiting_push_confirm` / HTTP 401:

1. `get_adapter_status` — bei `cooldown` warten; bei `awaiting_push_confirm` Schritt 2.
2. Wenn `status=awaiting_push_confirm`: Push ist **schon** raus. User bitten, in der
   Trade-Republic-App zu bestätigen. **Warten.**
3. Nach User-Bestätigung: **dasselbe MCP-Tool nochmal** aufrufen (pollt denselben
   `process_id`, finalisiert Cookies). Kein neues Login, kein anderes Tool.
4. `awaiting_authenticator` → Code vom User holen → **dasselbe Tool erneut**.

`awaiting_push_confirm` heißt **nicht** „fehlgeschlagen“ — es heißt „warte auf den Menschen“.

## Vor jedem riskanten Schritt

1. Fehler-JSON: `code`, `retry_after_seconds`, `guidance` befolgen.
2. Nie Push aus normalen Read-Loops (`TR_MCP_ALLOW_INTERACTIVE_LOGIN=0`).
3. Bei `write_backoff`: keine Mutationen; State mit Reads prüfen.
4. Keine anderen Market-Data-Provider für Depot/Konto.

## Flags (Default: aus)

| Env | Erlaubt |
|-----|---------|
| `TR_MCP_WRITE_ENABLED=1` | Watchlist add/remove |
| `TR_MCP_TRADING_ENABLED=1` | Limit / Stop-Market / Cancel (**echtes Geld**) |

## Reads — bevorzugte Reihenfolge

1. `get_adapter_status` (bei Fehlern / Unsicherheit)
2. `get_account_summary`
3. `list_active_positions` / `get_position_details`
4. `list_open_orders` / `list_order_history` / `get_order` / Sparpläne / Alarme
5. Timeline / Transaktionen bei Bedarf

Research: `search_instruments` → ISIN; Quotes/Analysen; vor Order
`get_order_preview` + `get_instrument_suitability`. Ticker = ISIN.

## Confirm-Token-Flow (Mutationen)

1. `confirmed=false` → deutsche `message` + `confirm_token`.
2. User klar zustimmen lassen.
3. Identische Parameter + `confirmed=true` + Token.
4. Bei `uncertain`: warten, State lesen (`get_watchlist` / `list_open_orders`), nicht sofort erneut mutieren.

## Trading

Nur bei explizitem User-Wunsch + `TR_MCP_TRADING_ENABLED=1`:
`place_limit_order`, `place_stop_market_order` (sell = Stop-Loss), `cancel_order`.

## Fehler-Codes

| code / status | Aktion |
|---------------|--------|
| `login_required` / `session_expired` | **Dasselbe Tool** erneut (Adapter erneuert Session) |
| `awaiting_push_confirm` | User bestätigt Push → **dasselbe Tool** erneut |
| `rate_limited` / `cooldown` | Warten `retry_after_seconds` |
| `write_backoff` | Warten; Reads statt Mutates |
| `writes_disabled` / `trading_disabled` | Flag fehlt — nicht umgehen |
| `confirmation_required_or_invalid` | Neu previewen mit `confirmed=false` |

## Verbote (allgemein)

- Keine Orders/Watchlist ohne explizite User-Zustimmung.
- Kein Trading „zum Testen“.
- Hermes-Namen: `mcp__<server>__<tool>` (z. B. `mcp__trade-republic__get_account_summary`).
