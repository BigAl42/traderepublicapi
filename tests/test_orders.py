"""Tests for order list extraction / normalization helpers."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "tr-adapter"
ROOT = Path(__file__).resolve().parent.parent
for path in (str(ADAPTER_DIR), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


class OrderHelpersTest(unittest.TestCase):
    def test_extract_orders_from_list_and_nested(self):
        from tr_client import TradeRepublicClient

        flat = [{"id": "a", "isin": "US0378331005"}]
        self.assertEqual(TradeRepublicClient._extract_orders(flat), flat)
        nested = {
            "open": [{"id": "1", "isin": "US0378331005", "type": "buy"}],
            "executed": [{"id": "2", "isin": "DE000BASF111", "type": "sell"}],
        }
        ids = {o["id"] for o in TradeRepublicClient._extract_orders(nested)}
        self.assertEqual(ids, {"1", "2"})

    def test_normalize_order_summary(self):
        from tr_client import TradeRepublicClient

        summary = TradeRepublicClient._normalize_order(
            {
                "orderId": "ord-9",
                "instrumentId": "us0378331005",
                "type": "Buy",
                "status": "OPEN",
                "size": "1.5",
                "limit": "190.0",
                "exchangeId": "LSX",
            }
        )
        self.assertEqual(summary["order_id"], "ord-9")
        self.assertEqual(summary["ticker"], "US0378331005")
        self.assertEqual(summary["side"], "buy")
        self.assertEqual(summary["status"], "OPEN")
        self.assertIn("raw", summary)


class OrderClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_open_orders_filters_ticker(self):
        from tr_client import TradeRepublicClient

        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "c.txt"
            circuit = Path(tmp) / "circuit.json"
            with patch("tr_client.TRApi") as api_cls:
                api = MagicMock()
                api.cookies_file = cookies
                api.reset_transport = AsyncMock()
                api_cls.return_value = api
                with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                    client = TradeRepublicClient(token="tok")
                    client._api = api
                    client._query_auth = AsyncMock(
                        return_value={
                            "orders": [
                                {"id": "1", "isin": "US0378331005", "type": "buy"},
                                {"id": "2", "isin": "DE000BASF111", "type": "sell"},
                            ]
                        }
                    )
                    result = await client.list_open_orders(ticker="US0378331005")
                    self.assertEqual(result["count"], 1)
                    self.assertEqual(result["orders"][0]["order_id"], "1")
                    self.assertEqual(result["ticker"], "US0378331005")

    async def test_get_order_finds_in_history(self):
        from tr_client import TradeRepublicClient

        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "c.txt"
            circuit = Path(tmp) / "circuit.json"
            with patch("tr_client.TRApi") as api_cls:
                api = MagicMock()
                api.cookies_file = cookies
                api.reset_transport = AsyncMock()
                api.timeline_detail_order = MagicMock()
                api_cls.return_value = api
                with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                    client = TradeRepublicClient(token="tok")
                    client._api = api

                    calls = {"n": 0}

                    async def side_effect(factory, *, mutating: bool = False):
                        calls["n"] += 1
                        if calls["n"] == 1:
                            return {"orders": []}
                        if calls["n"] == 2:
                            return {
                                "orders": [
                                    {
                                        "id": "ord-42",
                                        "isin": "US0378331005",
                                        "type": "buy",
                                        "status": "FILLED",
                                    }
                                ]
                            }
                        return {"sections": [{"title": "Order"}]}

                    client._query_auth = AsyncMock(side_effect=side_effect)
                    result = await client.get_order("ord-42")
                    self.assertEqual(result["order_id"], "ord-42")
                    self.assertEqual(result["found_in"], "history")
                    self.assertEqual(result["order"]["ticker"], "US0378331005")
                    self.assertIsNotNone(result["detail"])


if __name__ == "__main__":
    unittest.main()
