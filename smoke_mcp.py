#!/usr/bin/env python3
"""MCP adapter smoke checks for Hermes deploy validation.

Modes:
  --stdio (default)  MCP stdio plumbing with mocked TR client — no account needed
  --live             Read-only checks against Trade Republic (TR_TOKEN or TR_PHONE/PIN)

Examples:
  python3 smoke_mcp.py
  python3 smoke_mcp.py --stdio
  python3 smoke_mcp.py --live
  python3 smoke_mcp.py --live --env tr-adapter/.env
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
ADAPTER_DIR = REPO_ROOT / "tr-adapter"

for path in (str(REPO_ROOT), str(ADAPTER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trade Republic MCP smoke checks")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run read-only live checks against Trade Republic (default: stdio plumbing)",
    )
    parser.add_argument(
        "--env",
        default=str(REPO_ROOT / ".env"),
        help="Path to .env for --live mode",
    )
    parser.add_argument(
        "--adapter-entry",
        action="store_true",
        help="Use tr-adapter/ as MCP cwd instead of repo root",
    )
    args = parser.parse_args()

    if args.live:
        from dotenv import load_dotenv

        env_path = Path(args.env)
        if env_path.is_file():
            load_dotenv(env_path, override=True)
            print(f"[env] Loaded {env_path}")
        import os

        token = os.getenv("TR_TOKEN", "")
        phone = os.getenv("TR_PHONE", "")
        pin = os.getenv("TR_PIN", "")
        if not token and not (phone and pin):
            print("ERROR: --live requires TR_TOKEN or TR_PHONE+TR_PIN")
            return 1

    from smoke_tools import run_live_smoke, run_stdio_smoke

    try:
        if args.live:
            asyncio.run(run_live_smoke())
        else:
            asyncio.run(
                run_stdio_smoke(use_root_entrypoint=not args.adapter_entry)
            )
    except Exception as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        return 1

    print("SMOKE PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
