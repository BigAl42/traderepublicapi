"""Tests for structured MCP error payloads and adapter status."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "tr-adapter"
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

from errors import error_payload, raise_structured  # noqa: E402
from mcp_write import ConfirmationError, WriteToolsDisabledError  # noqa: E402
from session import ErrorKind  # noqa: E402
from tr_client import TradeRepublicClient, TradeRepublicClientError  # noqa: E402


class StructuredErrorTest(unittest.TestCase):
    def test_rate_limited_payload(self):
        exc = TradeRepublicClientError(
            "too many",
            kind=ErrorKind.RATE_LIMITED,
            retryable=False,
            retry_after_seconds=900,
        )
        payload = error_payload(exc)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "rate_limited")
        self.assertEqual(payload["retry_after_seconds"], 900)
        self.assertIn("guidance", payload)

    def test_raise_structured_is_json(self):
        with self.assertRaises(RuntimeError) as ctx:
            raise_structured(WriteToolsDisabledError("off"))
        data = json.loads(str(ctx.exception))
        self.assertEqual(data["code"], "writes_disabled")

    def test_confirmation_payload(self):
        payload = error_payload(ConfirmationError("missing token"))
        self.assertEqual(payload["code"], "confirmation_required_or_invalid")


class AdapterStatusTest(unittest.TestCase):
    def test_status_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "missing.txt"
            with patch.dict(
                "os.environ",
                {
                    "TR_TOKEN": "",
                    "TR_PHONE": "",
                    "TR_PIN": "",
                    "TR_COOKIES_FILE": str(cookies),
                    "TR_MCP_WRITE_ENABLED": "",
                    "TR_MCP_ALLOW_INTERACTIVE_LOGIN": "0",
                },
                clear=False,
            ):
                client = TradeRepublicClient(token=None)
                client._token = None
                client._has_credentials = False
                status = client.get_adapter_status()
                self.assertEqual(status["status"], "unconfigured")
                self.assertFalse(status["write_enabled"])
                self.assertFalse(status["allow_interactive_login"])
                self.assertFalse(status["auth_circuit_open"])

    def test_write_backoff_blocks_and_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "c.txt"
            with patch.dict(
                "os.environ",
                {
                    "TR_TOKEN": "tok",
                    "TR_COOKIES_FILE": str(cookies),
                    "TR_MCP_WRITE_VERIFY_BACKOFF_SEC": "120",
                },
                clear=False,
            ):
                client = TradeRepublicClient(token="tok")
                client._note_uncertain_write("add_to_watchlist")
                status = client.get_adapter_status()
                self.assertEqual(status["status"], "write_backoff")
                self.assertGreater(status["write_verify_backoff_remaining_seconds"], 0)
                with self.assertRaises(TradeRepublicClientError) as ctx:
                    client._guard_write_backoff("remove_from_watchlist")
                self.assertEqual(ctx.exception.retry_after_seconds, status["retry_after_seconds"])


if __name__ == "__main__":
    unittest.main()
