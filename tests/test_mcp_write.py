"""Tests for write confirmation tokens and gate."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "tr-adapter"
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

from mcp_write import (  # noqa: E402
    ConfirmationError,
    ConfirmationStore,
    confirmation_required,
    require_confirmation,
    write_enabled,
)


class WriteEnabledTest(unittest.TestCase):
    def test_defaults_off(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TR_MCP_WRITE_ENABLED", None)
            self.assertFalse(write_enabled())
        with patch.dict(os.environ, {"TR_MCP_WRITE_ENABLED": "1"}):
            self.assertTrue(write_enabled())


class ConfirmationStoreTest(unittest.TestCase):
    def test_issue_and_redeem_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfirmationStore(Path(tmp) / "confirmations.json")
            issued = store.issue("add_to_watchlist", "US0378331005")
            token = issued["confirm_token"]
            self.assertGreaterEqual(len(token), 16)
            store.redeem("add_to_watchlist", "US0378331005", token)
            with self.assertRaises(ConfirmationError):
                store.redeem("add_to_watchlist", "US0378331005", token)

    def test_bare_confirmed_without_token_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfirmationStore(Path(tmp) / "confirmations.json")
            with self.assertRaises(ConfirmationError) as ctx:
                store.redeem("add_to_watchlist", "US0378331005", None)
            self.assertIn("confirm_token", str(ctx.exception).lower())

    def test_action_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfirmationStore(Path(tmp) / "confirmations.json")
            token = store.issue("add_to_watchlist", "US0378331005")["confirm_token"]
            with self.assertRaises(ConfirmationError) as ctx:
                store.redeem("remove_from_watchlist", "US0378331005", token)
            self.assertIn("does not match", str(ctx.exception).lower())

    def test_expired_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "confirmations.json"
            store = ConfirmationStore(path)
            token = store.issue("add_to_watchlist", "X")["confirm_token"]
            data = json.loads(path.read_text(encoding="utf-8"))
            data["tokens"][token]["expires_at"] = time.time() - 1
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfirmationError):
                store.redeem("add_to_watchlist", "X", token)

    def test_confirmation_required_payload(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            store_path = str(Path(tmp) / "c.json")
            with patch.dict(os.environ, {"TR_MCP_CONFIRM_STORE": store_path}):
                # Reset module singleton path via new env — call helpers that use _STORE
                import mcp_write as mw

                mw._STORE = mw.ConfirmationStore(Path(store_path))
                payload = confirmation_required(
                    "add_to_watchlist", "US0378331005", instrument_name="Apple"
                )
                self.assertEqual(payload["status"], "confirmation_required")
                self.assertIn("confirm_token", payload)
                self.assertIn("Apple", payload["message"])
                require_confirmation(
                    "add_to_watchlist",
                    "US0378331005",
                    payload["confirm_token"],
                )


if __name__ == "__main__":
    unittest.main()
