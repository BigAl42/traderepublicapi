"""Offline tests for the root MCP server."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
    return mock


def _patch_client(mock: MagicMock):
    mcp_server = _fresh_import_mcp_server()
    mcp_server._client = mock
    return mcp_server


class RootMcpServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_account_summary(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        result = await mcp_server.mcp.call_tool("get_account_summary", {})
        self.assertTrue(result)
        mock.get_balance_info.assert_awaited_once()

    async def test_list_active_positions(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        result = await mcp_server.mcp.call_tool("list_active_positions", {})
        self.assertTrue(result)
        mock.get_holdings.assert_awaited_once()

    async def test_get_position_details(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("get_position_details", {"ticker": "US0378331005"})
        mock.get_ticker_details.assert_awaited_once_with("US0378331005")

    async def test_search_instruments(self):
        mock = _mock_client()
        mcp_server = _patch_client(mock)
        await mcp_server.mcp.call_tool("search_instruments", {"query": "Apple"})
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

    def test_ticker_validation(self):
        mcp_server = _fresh_import_mcp_server()
        parsed = mcp_server.TickerInput(ticker=" us0378331005 ")
        self.assertEqual(parsed.ticker, "US0378331005")
        with self.assertRaises(ValidationError):
            mcp_server.TickerInput(ticker="US-037833")

    async def test_stdio_roundtrip(self):
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
                cwd=str(ROOT),
                env={**os.environ, "PYTHONPATH": str(ROOT)},
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


if __name__ == "__main__":
    unittest.main()
