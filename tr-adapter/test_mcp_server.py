"""Offline tests and stdio smoke check for the Trade Republic MCP adapter."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

ADAPTER_DIR = Path(__file__).resolve().parent
ROOT = ADAPTER_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))


def _fresh_import_mcp_server():
    for name in list(sys.modules):
        if name in {"mcp_server", "tr_client"} or name.startswith("mcp_server."):
            del sys.modules[name]
    import mcp_server

    mcp_server._client = None
    return mcp_server


def _mock_client() -> MagicMock:
    mock = MagicMock()
    mock.get_balance_info = AsyncMock(return_value={
        "summary": {"total_cash": 1000.0, "buying_power": 900.0, "currency": "EUR"}
    })
    mock.get_holdings = AsyncMock(return_value=[
        {"ticker": "US0378331005", "name": "Apple", "quantity": 5, "profit_loss": 12.3}
    ])
    mock.get_ticker_details = AsyncMock(return_value={
        "ticker": "US0378331005",
        "instrument": {"name": "Apple"},
        "position": {"quantity": 5},
    })
    mock.search_instruments = AsyncMock(return_value={
        "query": "Apple",
        "results": [{"isin": "US0378331005", "name": "Apple Inc."}],
    })
    mock.get_price_history = AsyncMock(return_value={
        "ticker": "US0378331005",
        "range": "1y",
        "history": {"aggregates": []},
    })
    mock.get_stock_news = AsyncMock(return_value={
        "ticker": "US0378331005",
        "news": [{"headline": "Apple reports earnings"}],
    })
    mock.get_stock_analysis = AsyncMock(return_value={
        "ticker": "US0378331005",
        "details": {"name": "Apple"},
        "kpis": [],
        "dividends": [],
        "performance": {},
        "position": None,
    })
    mock.get_etf_analysis = AsyncMock(return_value={
        "ticker": "IE00B4L5Y983",
        "details": {"name": "MSCI World"},
        "composition": [],
        "position": None,
    })
    mock.get_crypto_analysis = AsyncMock(return_value={
        "ticker": "XC000A2P6LJ6",
        "details": {"name": "Bitcoin"},
        "position": None,
    })
    mock.get_portfolio_history = AsyncMock(return_value={
        "range": "1y",
        "history": {"aggregates": []},
    })
    mock.get_watchlist = AsyncMock(return_value={
        "watchlist": [{"isin": "US0378331005", "name": "Apple"}],
    })
    mock.get_recent_transactions = AsyncMock(return_value={
        "limit": 20,
        "count": 1,
        "transactions": [{"type": "timelineEvent", "title": "Buy"}],
    })
    mock.instrument_label = AsyncMock(return_value="Apple Inc.")
    mock.add_to_watchlist = AsyncMock(return_value={
        "status": "completed",
        "action": "add_to_watchlist",
        "ticker": "US0378331005",
    })
    mock.remove_from_watchlist = AsyncMock(return_value={
        "status": "completed",
        "action": "remove_from_watchlist",
        "ticker": "US0378331005",
    })
    return mock


def _patch_client(mock: MagicMock):
    mcp_server = _fresh_import_mcp_server()
    mcp_server._client = mock
    return mcp_server


class TickerInputTest(unittest.TestCase):
    def test_normalizes_isin(self):
        mcp_server = _fresh_import_mcp_server()
        parsed = mcp_server.TickerInput(ticker=" us0378331005 ")
        self.assertEqual(parsed.ticker, "US0378331005")

    def test_rejects_invalid_characters(self):
        mcp_server = _fresh_import_mcp_server()
        with self.assertRaises(ValidationError):
            mcp_server.TickerInput(ticker="US-037833")


class TradeRepublicClientTest(unittest.TestCase):
    def test_init_without_credentials_allowed_for_public_tools(self):
        _fresh_import_mcp_server()
        from tr_client import TradeRepublicClient

        with patch.dict(os.environ, {}, clear=True):
            client = TradeRepublicClient()
        self.assertFalse(client._has_credentials)

    def test_account_methods_require_credentials(self):
        _fresh_import_mcp_server()
        from tr_client import TradeRepublicClient, TradeRepublicClientError

        with patch.dict(os.environ, {}, clear=True):
            client = TradeRepublicClient()
            with self.assertRaises(TradeRepublicClientError) as ctx:
                client._ensure_session()
        self.assertIn("Missing credentials", str(ctx.exception))

    def test_normalize_position(self):
        from tr_client import TradeRepublicClient

        raw = {
            "isin": "US0378331005",
            "name": "Apple",
            "netSize": 10,
            "averageBuyIn": 150.0,
            "profitLoss": 25.5,
        }
        item = TradeRepublicClient._normalize_position(raw)
        self.assertEqual(item["ticker"], "US0378331005")
        self.assertEqual(item["quantity"], 10)
        self.assertEqual(item["profit_loss"], 25.5)

    def test_extract_timeline_items(self):
        from tr_client import TradeRepublicClient

        events = [{"id": "1"}, {"id": "2"}]
        self.assertEqual(TradeRepublicClient._extract_timeline_items(events), events)
        self.assertEqual(
            TradeRepublicClient._extract_timeline_items({"data": events}),
            events,
        )


class McpToolsTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_account_summary_via_call_tool(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        result = await mcp_server.mcp.call_tool("get_account_summary", {})
        self.assertTrue(result)
        mock.get_balance_info.assert_awaited_once()

    async def test_list_active_positions_via_call_tool(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        result = await mcp_server.mcp.call_tool("list_active_positions", {})
        self.assertTrue(result)
        mock.get_holdings.assert_awaited_once()

    async def test_get_position_details_validates_ticker(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("get_position_details", {"ticker": "US0378331005"})
        mock.get_ticker_details.assert_awaited_once_with("US0378331005", include_position=True)

    async def test_get_stock_analysis(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("get_stock_analysis", {"ticker": "US0378331005"})
        mock.get_stock_analysis.assert_awaited_once_with("US0378331005", include_position=False)

    async def test_get_etf_analysis(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("get_etf_analysis", {"ticker": "IE00B4L5Y983"})
        mock.get_etf_analysis.assert_awaited_once_with("IE00B4L5Y983", include_position=False)

    async def test_get_crypto_analysis(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("get_crypto_analysis", {"ticker": "XC000A2P6LJ6"})
        mock.get_crypto_analysis.assert_awaited_once_with("XC000A2P6LJ6", include_position=False)

    async def test_api_error_returns_structured_message(self):
        mcp_server = _fresh_import_mcp_server()
        error_cls = mcp_server.TradeRepublicClientError

        mock = MagicMock()
        mock.get_balance_info = AsyncMock(
            side_effect=error_cls(
                "Session expired or invalid TR_TOKEN.", retryable=True
            )
        )
        mcp_server._client = mock
        with self.assertRaises(Exception) as ctx:
            await mcp_server.mcp.call_tool("get_account_summary", {})
        self.assertIn("session problem", str(ctx.exception).lower())

    async def test_search_instruments(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool(
            "search_instruments",
            {"query": "Apple", "instrument_type": "stock", "jurisdiction": "DE"},
        )
        mock.search_instruments.assert_awaited_once()

    async def test_get_price_history(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool(
            "get_price_history",
            {"ticker": "US0378331005", "range": "1y"},
        )
        mock.get_price_history.assert_awaited_once()

    async def test_get_stock_news(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("get_stock_news", {"ticker": "US0378331005"})
        mock.get_stock_news.assert_awaited_once_with("US0378331005")

    async def test_get_portfolio_history(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("get_portfolio_history", {"range": "1y"})
        mock.get_portfolio_history.assert_awaited_once_with("1y")

    async def test_get_watchlist(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("get_watchlist", {})
        mock.get_watchlist.assert_awaited_once()

    async def test_get_recent_transactions(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("get_recent_transactions", {"limit": 10})
        mock.get_recent_transactions.assert_awaited_once_with(limit=10, after=None)


class WatchlistWriteTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _tool_text(result) -> str:
        return "".join(block.text for block in result if hasattr(block, "text"))

    async def test_add_blocked_without_write_flag(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        with patch.dict(os.environ, {"TR_MCP_WRITE_ENABLED": ""}, clear=False):
            with self.assertRaises(Exception) as ctx:
                await mcp_server.mcp.call_tool("add_to_watchlist", {"ticker": "US0378331005"})
        self.assertIn("write tools disabled", str(ctx.exception).lower())
        mock.add_to_watchlist.assert_not_called()

    async def test_add_requires_confirmation(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        with patch.dict(os.environ, {"TR_MCP_WRITE_ENABLED": "1"}):
            result = await mcp_server.mcp.call_tool(
                "add_to_watchlist",
                {"ticker": "US0378331005", "confirmed": False},
            )
        mock.add_to_watchlist.assert_not_called()
        self.assertIn("confirmation_required", self._tool_text(result))
        self.assertIn("US0378331005", self._tool_text(result))

    async def test_add_executes_after_confirmation(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        with patch.dict(os.environ, {"TR_MCP_WRITE_ENABLED": "1"}):
            await mcp_server.mcp.call_tool(
                "add_to_watchlist",
                {"ticker": "US0378331005", "confirmed": True},
            )
        mock.add_to_watchlist.assert_awaited_once_with("US0378331005")

    async def test_remove_executes_after_confirmation(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        with patch.dict(os.environ, {"TR_MCP_WRITE_ENABLED": "1"}):
            await mcp_server.mcp.call_tool(
                "remove_from_watchlist",
                {"ticker": "US0378331005", "confirmed": True},
            )
        mock.remove_from_watchlist.assert_awaited_once_with("US0378331005")


class McpWriteHelpersTest(unittest.TestCase):
    def test_confirmation_message_add(self):
        from mcp_write import confirmation_required

        payload = confirmation_required("add_to_watchlist", "US0378331005", instrument_name="Apple")
        self.assertEqual(payload["status"], "confirmation_required")
        self.assertIn("Apple", payload["message"])
        self.assertIn("US0378331005", payload["message"])


class StdioSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_tool_roundtrip(self):
        """Start the MCP server over stdio, call one tool, verify the response."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        bootstrap = """
import os, sys
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TR_TOKEN", "offline-test-token")
mock = MagicMock()
mock.get_balance_info = AsyncMock(return_value={
    "summary": {"total_cash": 123.45, "buying_power": 100.0, "currency": "EUR"}
})
patch("mcp_server.get_client", return_value=mock).start()

from mcp_server import mcp
mcp.run(transport="stdio")
"""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write(bootstrap)
            bootstrap_path = handle.name

        try:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-u", bootstrap_path],
                cwd=str(ADAPTER_DIR),
                env={**os.environ, "PYTHONPATH": f"{ROOT}:{ADAPTER_DIR}"},
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    self.assertIn("get_account_summary", names)

                    result = await session.call_tool("get_account_summary", {})
                    self.assertFalse(result.isError)
                    payload = "".join(
                        block.text for block in result.content if hasattr(block, "text")
                    )
                    self.assertIn("123.45", payload)
        finally:
            Path(bootstrap_path).unlink(missing_ok=True)


async def run_smoke_cli() -> int:
    """CLI entry: run stdio smoke test (used by CI or manual checks)."""
    case = StdioSmokeTest()
    try:
        await case.test_stdio_tool_roundtrip()
    except Exception as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1
    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        raise SystemExit(asyncio.run(run_smoke_cli()))
    unittest.main()
