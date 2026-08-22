# Trade Republic MCP adapter (Hermes)

Read-first MCP server over the unofficial Trade Republic API. Mutating tools are
limited to watchlist add/remove and are **off by default**.

## Production defaults

```bash
TR_MCP_ALLOW_INTERACTIVE_LOGIN=0
TR_MCP_WRITE_ENABLED=0
# Provide TR_TOKEN and/or a warm cookie file:
# TR_COOKIES_FILE=tr_cookies.txt
```

Warm the session **offline** (not from Hermes tool loops):

```bash
python3 check_login.py
# or: python3 check_login.py --env tr-adapter/.env
```

## Ops: cooldown and status

When Trade Republic rate-limits auth, the adapter opens a file-backed circuit
(`*.auth_circuit.json`) and refuses new logins.

1. Call `get_adapter_status` (local, no TR network call).
2. If `auth_circuit_open` / `status=cooldown`, wait `retry_after_seconds`.
3. Do **not** enable interactive login or spam `check_login.py`.
4. Resume from `TR_TOKEN` / cookies after cooldown.

Tool errors are JSON in the error message:

```json
{
  "status": "error",
  "code": "rate_limited",
  "message": "...",
  "retryable": false,
  "retry_after_seconds": 900,
  "guidance": "..."
}
```

## Watchlist writes (confirm_token)

Requires `TR_MCP_WRITE_ENABLED=1`.

1. `add_to_watchlist(ticker, confirmed=false)`  
   → `confirmation_required` + German `message` + one-time `confirm_token`
2. Ask the user explicitly.
3. On clear consent:  
   `add_to_watchlist(ticker, confirmed=true, confirm_token=<token>)`  
   Bare `confirmed=true` without the token is rejected.

After mutate, the adapter re-fetches the watchlist:

| Result | Meaning |
|--------|---------|
| `completed` + `verified=true` | Membership matches intent |
| `uncertain` / verify failed | Wait `retry_after_seconds` (default 60); call `get_watchlist` / `get_adapter_status`; do not immediately mutate again |

## Run

```bash
# Docker / repo root entrypoint (re-exports tr-adapter)
python3 mcp_server.py

# Or directly
python3 tr-adapter/mcp_server.py
```

Offline tests: `make test`
