"""Offline tests for MCP smoke helpers."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "tr-adapter"
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

from smoke_tools import EXPECTED_MCP_TOOLS, build_mock_client, run_stdio_smoke  # noqa: E402


class SmokeToolsTest(unittest.TestCase):
    def test_expected_tool_count(self):
        self.assertEqual(len(EXPECTED_MCP_TOOLS), 30)
        self.assertIn("get_adapter_status", EXPECTED_MCP_TOOLS)
        self.assertIn("renew_session", EXPECTED_MCP_TOOLS)
        self.assertIn("add_to_watchlist", EXPECTED_MCP_TOOLS)
        self.assertIn("get_transaction_detail", EXPECTED_MCP_TOOLS)
        self.assertIn("list_open_orders", EXPECTED_MCP_TOOLS)
        self.assertIn("list_savings_plans", EXPECTED_MCP_TOOLS)
        self.assertIn("list_price_alarms", EXPECTED_MCP_TOOLS)
        self.assertIn("get_live_quote", EXPECTED_MCP_TOOLS)
        self.assertIn("get_order_preview", EXPECTED_MCP_TOOLS)
        self.assertIn("get_account_pairs", EXPECTED_MCP_TOOLS)
        self.assertIn("search_instruments_aggregations", EXPECTED_MCP_TOOLS)
        self.assertIn("get_search_tags", EXPECTED_MCP_TOOLS)
        self.assertIn("get_search_suggested_tags", EXPECTED_MCP_TOOLS)

    def test_mock_client_has_status(self):
        mock = build_mock_client()
        status = mock.get_adapter_status()
        self.assertEqual(status["status"], "cold")


class StdioSmokeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_smoke_root_entrypoint(self):
        await run_stdio_smoke(use_root_entrypoint=True)


if __name__ == "__main__":
    unittest.main()
