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

   Expect **30 tools** and OK for `get_adapter_status`, `get_account_summary`,
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
TR_MCP_AUTO_RENEW=1               # soft cookie/token renew on 401 (default on)
TR_MCP_RENEW_ALLOW_PUSH=1         # renew_session may start app push when cookies dead
TR_TOKEN=...                      # or warm TR_COOKIES_FILE via check_login.py / renew_session
# Optional: pin cookie/circuit/confirm files when Hermes cwd varies
# TR_ADAPTER_DATA_DIR=/opt/data/home/traderepublicapi/tr-adapter
```

Warm session offline — not from agent login loops:

```bash
python3 check_login.py
```

Or from Hermes: call MCP tool `renew_session` (confirm push in the app when asked).

## MCP tools (30)

| Tool | Auth | Notes |
|------|------|--------|
| `get_adapter_status` | no TR call | **Call first** on errors / cooldown |
| `renew_session` | soft / push | **Cold 401 fix** — confirm app push when asked |
| `get_account_summary` | yes | Cash / buying power |
| `list_active_positions` | yes | Holdings |
| `get_position_details` | yes | One ISIN |
| `get_stock_analysis` | yes | Fundamentals |
| `get_etf_analysis` | yes | ETF composition |
| `get_crypto_analysis` | yes | Crypto details |
| `search_instruments` | no | Find ISINs |
| `search_instruments_aggregations` | no | Faceted search counts |
| `get_search_tags` | no | Available search tags |
| `get_search_suggested_tags` | no | Tag suggestions for query |
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
4. **Never** switch to ClawStreet / other providers for TR account or portfolio
   tools. Soft auto-renew runs on 401; if cookies are fully dead call
   `renew_session` (app push). Do not invent browser cookie scraping.

### When `code` is `login_required` or `session_expired`

- Soft auto-renew already attempted.
- Call `renew_session` (not other providers).
- If `status=awaiting_push_confirm`: tell the user to confirm in the Trade Republic
  app, then call `renew_session` again.
- If `status=ready`: retry the **same** original account tool once.
- Call `get_adapter_status` to inspect `last_renew_result` / `last_renew_http_status`.

### When charts/search return 401 but no account is needed

Dead `tr_session` cookies used to poison the public WebSocket connect. The adapter
now drops the in-memory session cookie and retries anonymously. Account tools still
need `renew_session` / fresh cookies.

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

## Hermes MCP compatibility (research notes)

What Hermes expects from an MCP server, and how it relates to the
`missing required argument(s): ticker` failures seen via the gateway
(but not via `smoke_mcp.py`).

### What Hermes requires (server side)

Hermes is a standard MCP **client**. There is no Hermes-specific server
protocol — any compliant MCP server over stdio or HTTP works.

| Requirement | Detail |
|-------------|--------|
| Transport | stdio (`command` + `args`) or HTTP (`url`) in `~/.hermes/config.yaml` → `mcp_servers` |
| Discovery | `initialize` + `tools/list` at startup |
| Tool naming | Hermes registers tools as `mcp_{server}_{tool}` (hyphens/dots → underscores) |
| `inputSchema` | Valid JSON Schema object; Hermes sanitizes on registration (`schema_sanitizer.py`) |
| Env isolation | stdio subprocesses get a filtered env; secrets only via explicit `env:` block |

Docs: [Hermes MCP feature guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp),
[native MCP reference](https://github.com/NousResearch/hermes-agent/blob/main/skills/autonomous-ai-agents/hermes-agent/references/native-mcp.md).

Our adapter already meets these: FastMCP stdio, flat object schemas, simple
types (`string`, `boolean`, `integer`), `required` arrays set correctly.
`smoke_mcp.py --stdio` proves the server side is fine.

### Tool Search bridge (likely root cause of arg failures)

When MCP tools are present, Hermes **Tool Search** (default `tools.tool_search.enabled: auto`)
replaces direct MCP tool schemas with three bridge tools:

```
tool_search(query, limit?)
tool_describe(name)
tool_call(name, arguments)    ← nested arguments object
```

The model must call `tool_call("mcp_trade_republic_get_stock_news", {"ticker": "US0378331005"})`.
Hermes unwraps the bridge and forwards flat args to `session.call_tool()`.

**Known Hermes bugs that match our symptoms** (parameterless tools OK, required args “missing”):

1. **Flattened bridge arguments** ([hermes-agent#76650](https://github.com/NousResearch/hermes-agent/pull/76650)):
   Gemini/Copilot models emit `arguments.ticker` as a dotted sibling key instead of
   `arguments: {ticker: ...}`. Hermes reads `args.get("arguments")` as `None`, validates
   against `{}`, and returns `missing required argument(s): ticker` **before** our server
   is called.

2. **Blind `tool_call` without prior `tool_describe`** ([hermes-agent#59267](https://github.com/NousResearch/hermes-agent/pull/59267)):
   Model invokes deferred tools without loading the schema first.

3. **Truncated tool JSON** ([hermes-agent#35151](https://github.com/NousResearch/hermes-agent/issues/35151)):
   Broken argument JSON can be replaced with `{}` (newer main fails closed instead).

**Conclusion:** If `smoke_mcp.py` passes parameterized calls but Hermes gateway fails with
“missing required argument(s)”, the break is in Hermes’ bridge/validation layer, not in
this adapter’s schemas or FastMCP handlers.

### Hermes-side mitigations

1. **Upgrade Hermes** to a build that includes PR #76650 (flattened `arguments.*` recovery).
2. **Agent prompt:** For deferred MCP tools, always `tool_describe(name)` before
   `tool_call(name, arguments)` with a nested `arguments` object (not flat top-level keys).
3. **Disable Tool Search** for small catalogs (forces eager/direct tool schemas):
   ```yaml
   tools:
     tool_search:
       enabled: off
   ```
4. **Check model/provider:** Flattening bugs are reported mainly with Gemini-family models
   via Copilot; Claude/OpenAI nested objects usually work.

### Adapter-side best practices (optional hardening)

These do **not** fix the bridge flattening bug but reduce other friction:

| Change | Why |
|--------|-----|
| Avoid `str \| None` / `anyOf` for optionals | Use `default=""` instead of nullable `confirm_token` / `after` — simpler for strict parsers |
| Add `Field(description=...)` on every property | Helps Tool Search BM25 retrieval; our `ticker` fields currently only have `title` |
| Accept `isin` alias alongside `ticker` | Models often say “ISIN” but schema says `ticker` |
| Keep flat top-level params | No nested input objects; Hermes forwards flat dicts to MCP |
| Short tool docstrings | Descriptions feed search/describe; keep first sentence ≤60 chars for tier-1 listing |

Current schemas (verified via `mcp.list_tools()`): all parameterized tools use flat
`properties` + `required`; only `confirm_token` and `after` use `anyOf` nullable unions.

### Diagnosis checklist

| Test | Pass | Fail → |
|------|------|--------|
| `python3 smoke_mcp.py --stdio` | Server OK | Fix adapter / deploy |
| `python3 smoke_mcp.py --live` with args | TR + args OK | Session / TR API |
| Hermes: `get_adapter_status` (no args) | Bridge + MCP OK | Config / watchdog / slug |
| Hermes: `get_stock_news(ticker=…)` | End-to-end OK | Tool Search / model / Hermes version |
| Hermes logs: `session.call_tool` reached? | Adapter issue | Hermes bridge dropped args earlier |

## Troubleshooting

| Symptom | Likely cause | Action |
|---------|----------------|--------|
| Only 2 tools in agent | Stale gateway catalog / allowlist | Restart watchdog; run `smoke_mcp.py --stdio` |
| `missing required argument(s): ticker` via Hermes, smoke OK | Tool Search bridge / flattened args | Upgrade Hermes; try `tool_search.enabled: off`; see section above |
| `This event loop is already running` in `mcp-stderr.log` | Sync `TrBlockingApi` / `run_until_complete` inside FastMCP loop | Deploy current adapter (`TRApi` async only). Never point Hermes at scripts that use `TrBlockingApi`. |
| Tools missing after runtime errors | Hermes drops/fails MCP server after tool errors | Fix loop bug, restart watchdog, re-run `python3 smoke_mcp.py` |
| `login_required` | Cold session | Call `renew_session` (app push); or `check_login.py` / fresh `TR_TOKEN` |

| `writes_disabled` | Expected in prod | Set `TR_MCP_WRITE_ENABLED=1` only if needed |
| JSON error with `guidance` | Structured adapter error | Follow `guidance`; respect `retry_after_seconds` |

## Docker

Root `mcp_server.py` re-exports `tr-adapter/mcp_server.py` — use either as
Hermes command; working directory should be repo root with `.env` mounted.
