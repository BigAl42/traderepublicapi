"""Shared helpers for MCP smoke checks (stdio plumbing + optional live reads)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_DIR = Path(__file__).resolve().parent

# Keep in sync with @mcp.tool() registrations in tr-adapter/mcp_server.py
EXPECTED_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "get_adapter_status",
        "get_account_summary",
        "list_active_positions",
        "get_position_details",
        "get_stock_analysis",
        "get_etf_analysis",
        "get_crypto_analysis",
        "search_instruments",
        "search_instruments_aggregations",
        "get_search_tags",
        "get_search_suggested_tags",
        "get_price_history",
        "get_stock_news",
        "get_portfolio_history",
        "get_watchlist",
        "get_recent_transactions",
        "get_full_timeline",
        "get_transaction_detail",
        "list_open_orders",
        "list_savings_plans",
        "list_price_alarms",
        "get_live_quote",
        "get_derivatives",
        "get_instrument_suitability",
        "get_order_preview",
        "get_account_settings",
        "get_account_pairs",
        "add_to_watchlist",
        "remove_from_watchlist",
    }
)


def build_mock_client() -> MagicMock:
    """Minimal client mock for stdio plumbing (no Trade Republic network)."""
    mock = MagicMock()
    mock.get_adapter_status = MagicMock(
        return_value={
            "status": "cold",
            "session_ready": False,
            "write_enabled": False,
            "auth_circuit_open": False,
            "retry_after_seconds": None,
            "guidance": "smoke test mock",
        }
    )
    mock.get_balance_info = AsyncMock(
        return_value={
            "summary": {"total_cash": 123.45, "buying_power": 100.0, "currency": "EUR"}
        }
    )
    mock.search_instruments = AsyncMock(
        return_value={
            "query": "Apple",
            "results": [{"isin": "US0378331005", "name": "Apple Inc."}],
        }
    )
    mock.instrument_label = AsyncMock(return_value="Apple Inc.")
    return mock


def _stdio_bootstrap_source(patch_target: str) -> str:
    return f"""
import os
from unittest.mock import patch
from smoke_tools import build_mock_client

os.environ.setdefault("TR_TOKEN", "offline-smoke-token")
patch("{patch_target}", return_value=build_mock_client()).start()

from mcp_server import mcp
mcp.run(transport="stdio")
"""


async def run_stdio_smoke(*, use_root_entrypoint: bool = True) -> None:
    """Start MCP over stdio with a mocked client; verify tool catalog + sample calls."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if use_root_entrypoint:
        cwd = REPO_ROOT
        patch_target = "mcp_server.get_client"
        pythonpath = f"{REPO_ROOT}:{ADAPTER_DIR}"
    else:
        cwd = ADAPTER_DIR
        patch_target = "mcp_server.get_client"
        pythonpath = f"{REPO_ROOT}:{ADAPTER_DIR}"

    bootstrap = _stdio_bootstrap_source(patch_target)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(bootstrap)
        bootstrap_path = handle.name

    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-u", bootstrap_path],
            cwd=str(cwd),
            env={**os.environ, "PYTHONPATH": pythonpath},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                missing = EXPECTED_MCP_TOOLS - names
                extra = names - EXPECTED_MCP_TOOLS
                if missing:
                    raise RuntimeError(
                        f"MCP tools missing ({len(missing)}): {sorted(missing)}"
                    )
                if extra:
                    raise RuntimeError(
                        f"Unexpected extra MCP tools ({len(extra)}): {sorted(extra)}"
                    )
                print(f"[stdio] list_tools OK — {len(names)} tools")

                status = await session.call_tool("get_adapter_status", {})
                if status.isError:
                    raise RuntimeError(f"get_adapter_status failed: {status.content}")
                print("[stdio] get_adapter_status OK")

                summary = await session.call_tool("get_account_summary", {})
                if summary.isError:
                    raise RuntimeError(f"get_account_summary failed: {summary.content}")
                text = "".join(
                    block.text for block in summary.content if hasattr(block, "text")
                )
                if "123.45" not in text:
                    raise RuntimeError("get_account_summary mock payload not returned")
                print("[stdio] get_account_summary OK")

                search = await session.call_tool(
                    "search_instruments",
                    {"query": "Apple", "instrument_type": "stock", "jurisdiction": "DE"},
                )
                if search.isError:
                    raise RuntimeError(f"search_instruments failed: {search.content}")
                print("[stdio] search_instruments OK")
    finally:
        Path(bootstrap_path).unlink(missing_ok=True)


async def run_live_smoke() -> None:
    """Authenticated read smoke against Trade Republic (requires credentials)."""
    from tr_client import TradeRepublicClient, TradeRepublicClientError

    client = TradeRepublicClient()
    status = client.get_adapter_status()
    print("[live] get_adapter_status:")
    print(json.dumps(status, indent=2, ensure_ascii=False))

    if status.get("auth_circuit_open"):
        remaining = status.get("auth_cooldown_remaining_seconds", 0)
        raise RuntimeError(
            f"Auth circuit open — wait {remaining}s before live smoke (no login spam)."
        )

    print("[live] search_instruments (public) …")
    try:
        found = await client.search_instruments("Apple", page_size=3)
        count = len(found.get("results") or [])
        print(f"[live] search_instruments OK — {count} result(s)")
    except TradeRepublicClientError as exc:
        raise RuntimeError(f"search_instruments failed: {exc}") from exc

    print("[live] get_balance_info …")
    try:
        client._ensure_session()
        summary = await client.get_balance_info()
        cash = (summary.get("summary") or {}).get("total_cash")
        print(f"[live] get_balance_info OK — cash={cash}")
    except TradeRepublicClientError as exc:
        raise RuntimeError(f"get_balance_info failed: {exc}") from exc

    print("[live] get_watchlist …")
    try:
        wl = await client.get_watchlist()
        items = wl.get("watchlist")
        n = len(items) if isinstance(items, list) else "?"
        print(f"[live] get_watchlist OK — items={n}")
    except TradeRepublicClientError as exc:
        raise RuntimeError(f"get_watchlist failed: {exc}") from exc

    print("[live] All live read checks passed.")
