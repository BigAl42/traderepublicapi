# Hermes integration guide

How to run and operate the Trade Republic MCP adapter behind Hermes /
`mcp_stdio_watchdog`. For adapter internals see [README.md](README.md).

## Deploy checklist

After config change or image update:

1. Restart MCP server **and** watchdog (Hermes reload).
2. Confirm server slug matches config (e.g. `trade-republic`, not typos).
3. Run offline plumbing smoke (no TR account):

   ```bash
   python3 smoke_mcp.py --stdio
   ```

   Expect **26 tools** and OK for `get_adapter_status`, `get_account_summary`,
   `search_instruments`.

4. Optional live read smoke (credentials required):

   ```bash
   python3 check_login.py          # warm session first
   python3 smoke_mcp.py --live
   ```

## Recommended environment

```bash
TR_MCP_ALLOW_INTERACTIVE_LOGIN=0
TR_MCP_WRITE_ENABLED=0
TR_TOKEN=...                      # or warm TR_COOKIES_FILE via check_login.py
```

Warm session offline — not from agent login loops:

```bash
python3 check_login.py
```

## MCP tools (26)

| Tool | Auth | Notes |
|------|------|--------|
| `get_adapter_status` | no TR call | **Call first** on errors / cooldown |
| `get_account_summary` | yes | Cash / buying power |
| `list_active_positions` | yes | Holdings |
| `get_position_details` | yes | One ISIN |
| `get_stock_analysis` | yes | Fundamentals |
| `get_etf_analysis` | yes | ETF composition |
| `get_crypto_analysis` | yes | Crypto details |
| `search_instruments` | no | Find ISINs |
| `get_price_history` | no | Charts |
| `get_stock_news` | no | News |
| `get_portfolio_history` | yes | Depot history |
| `get_watchlist` | yes | Watchlist |
| `get_recent_transactions` | yes | Cash-relevant timeline subset |
| `get_full_timeline` | yes | Full timeline (broader) |
| `get_transaction_detail` | yes | Event detail / documents |
| `list_open_orders` | yes | Open (or terminated) orders |
| `list_savings_plans` | yes | Savings plans |
| `list_price_alarms` | yes | Price alarms |
| `get_live_quote` | no | One-shot live quote |
| `get_derivatives` | yes | Warrants / KOs / factors |
| `get_instrument_suitability` | yes | Pre-trade suitability |
| `get_order_preview` | yes | Price + available size (no order) |
| `get_account_settings` | yes | Account settings |
| `get_account_pairs` | yes | Depot / cash account numbers |
| `add_to_watchlist` | yes + write flag | Mutating; confirm_token flow |
| `remove_from_watchlist` | yes + write flag | Mutating; confirm_token flow |

Hermes exposes these as `mcp__<server_name>__<tool_name>` (e.g.
`mcp__trade-republic__get_adapter_status`).

## Agent policy (mandatory behaviour)

Paste or adapt into the Hermes agent system prompt.

### On any tool error

1. Parse the error JSON if present (`code`, `retry_after_seconds`, `guidance`).
2. Call `get_adapter_status` before retrying anything else.
3. **Never** trigger push login from the agent when
   `TR_MCP_ALLOW_INTERACTIVE_LOGIN=0` (production default).

### When `code` is `rate_limited` or status is `cooldown`

- Stop all TR tool calls until `retry_after_seconds` elapsed.
- Do not run `check_login.py` in a loop.
- Inform the user: auth cooldown; reads may resume after wait.

### When status is `write_backoff`

- Do not call `add_to_watchlist` / `remove_from_watchlist` until backoff ends.
- Use `get_watchlist` to inspect state instead of repeating the write.

### Watchlist mutations

Only when `TR_MCP_WRITE_ENABLED=1` and user explicitly asked:

1. `add_to_watchlist` / `remove_from_watchlist` with `confirmed=false` → get
   `confirm_token` + German `message`.
2. Ask user clearly; on consent only: retry with `confirmed=true` and
   `confirm_token`.
3. If result is `uncertain` or verify failed: wait `retry_after_seconds`,
   check watchlist, do **not** immediately mutate again.

### Preferred read order for portfolio questions

1. `get_adapter_status` (if prior errors)
2. `get_account_summary`
3. `list_active_positions` / `get_position_details` as needed
4. `list_open_orders` / `list_savings_plans` / `list_price_alarms` when relevant
5. `get_live_quote` / `get_order_preview` / `get_derivatives` for research & pre-trade
6. `get_recent_transactions` or `get_full_timeline`; then `get_transaction_detail` for docs

## Troubleshooting

| Symptom | Likely cause | Action |
|---------|----------------|--------|
| Only 2 tools in agent | Stale gateway catalog / allowlist | Restart watchdog; run `smoke_mcp.py --stdio` |
| `login_required` | Cold session | `check_login.py` offline; set `TR_TOKEN` |
| `writes_disabled` | Expected in prod | Set `TR_MCP_WRITE_ENABLED=1` only if needed |
| JSON error with `guidance` | Structured adapter error | Follow `guidance`; respect `retry_after_seconds` |

## Docker

Root `mcp_server.py` re-exports `tr-adapter/mcp_server.py` — use either as
Hermes command; working directory should be repo root with `.env` mounted.
