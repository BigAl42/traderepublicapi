#!/usr/bin/env python3
"""Quick login and connectivity check for the Trade Republic MCP adapter.

Usage:
    python3 check_login.py           # reads .env from repo root
    python3 check_login.py --env tr-adapter/.env
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — works whether run from repo root or tr-adapter/
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
ADAPTER_DIR = REPO_ROOT / "tr-adapter"

for _p in (str(REPO_ROOT), str(ADAPTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Trade Republic login / connectivity check")
parser.add_argument(
    "--env",
    default=str(REPO_ROOT / ".env"),
    help="Path to .env file (default: .env in repo root)",
)
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
from dotenv import load_dotenv  # noqa: E402

env_path = Path(args.env)
if env_path.exists():
    load_dotenv(env_path, override=True)
    print(f"[env]  Loaded {env_path}")
else:
    print(f"[env]  {env_path} not found — using existing environment variables")

import os  # noqa: E402

token  = os.getenv("TR_TOKEN", "")
phone  = os.getenv("TR_PHONE", "")
pin    = os.getenv("TR_PIN", "")
locale = os.getenv("TR_LOCALE", "de")

print()
print("[env]  TR_TOKEN  =", f"{token[:8]}…{token[-4:]}" if len(token) > 12 else (token or "(not set)"))
print("[env]  TR_PHONE  =", phone or "(not set)")
print("[env]  TR_PIN    =", "*" * len(pin) if pin else "(not set)")
print("[env]  TR_LOCALE =", locale)
print()

if not token and not (phone and pin):
    print("ERROR: No credentials found.")
    print("       Set TR_TOKEN  (session cookie from browser)  OR")
    print("       Set TR_PHONE + TR_PIN  (triggers push in TR app)")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Instantiate client
# ---------------------------------------------------------------------------
try:
    from tr_client import TradeRepublicClient, TradeRepublicClientError
except ImportError as exc:
    print(f"ERROR: Could not import tr_client: {exc}")
    print("       Run:  pip install -r requirements.txt")
    sys.exit(1)

import asyncio  # noqa: E402


async def _run_checks():
    print("[1/4]  Creating client …")
    try:
        client = TradeRepublicClient()
    except TradeRepublicClientError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    # -------------------------------------------------------------------
    # Trigger login / session resume
    # -------------------------------------------------------------------
    print("[2/4]  Connecting to Trade Republic …")
    if token:
        print("       Using TR_TOKEN (session resume — no push needed)")
    else:
        print("       Using TR_PHONE + TR_PIN")
        print("       >>> Open the Trade Republic app and CONFIRM the login push! <<<")

    try:
        await client._ensure_session()
        print("[2/4]  Session established ✓")
    except TradeRepublicClientError as exc:
        print(f"ERROR: Login failed: {exc}")
        sys.exit(1)

    # -------------------------------------------------------------------
    # Fetch account summary
    # -------------------------------------------------------------------
    print("[3/4]  Fetching account summary …")
    try:
        summary = await client.get_balance_info()
        s = summary.get("summary", {})
        print(f"       Cash:        {s.get('total_cash', '?')} {s.get('currency', '')}")
        print(f"       Buying power:{s.get('buying_power', '?')} {s.get('currency', '')}")
        print("[3/4]  Account summary ✓")
    except TradeRepublicClientError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    # -------------------------------------------------------------------
    # Fetch holdings
    # -------------------------------------------------------------------
    print("[4/4]  Fetching portfolio positions …")
    try:
        positions = await client.get_holdings()
        if positions:
            for pos in positions[:5]:
                ticker = pos.get("ticker", "?")
                name   = pos.get("name") or ticker
                qty    = pos.get("quantity", "?")
                pl     = pos.get("profit_loss")
                pl_str = f"  P/L: {pl}" if pl is not None else ""
                print(f"       {ticker}  {name}  qty={qty}{pl_str}")
            if len(positions) > 5:
                print(f"       … and {len(positions) - 5} more positions")
        else:
            print("       (no positions found)")
        print("[4/4]  Portfolio ✓")
    except TradeRepublicClientError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print()
    print("All checks passed — MCP adapter is ready to use.")


asyncio.run(_run_checks())
