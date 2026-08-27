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
    _confirm_store_path,
    confirmation_required,
    normalize_binding,
    order_confirmation_message,
    require_confirmation,
    trading_enabled,
    write_enabled,
)


class WriteEnabledTest(unittest.TestCase):
    def test_defaults_off(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TR_MCP_WRITE_ENABLED", None)
            os.environ.pop("TR_MCP_TRADING_ENABLED", None)
            self.assertFalse(write_enabled())
            self.assertFalse(trading_enabled())
        with patch.dict(os.environ, {"TR_MCP_WRITE_ENABLED": "1"}):
            self.assertTrue(write_enabled())
        with patch.dict(os.environ, {"TR_MCP_TRADING_ENABLED": "1"}):
            self.assertTrue(trading_enabled())


class ConfirmStorePathTest(unittest.TestCase):
    def test_override_resolves_absolute(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "confirmations.json")
            with patch.dict(os.environ, {"TR_MCP_CONFIRM_STORE": store}):
                path = _confirm_store_path()
                self.assertTrue(path.is_absolute())
                self.assertEqual(path, Path(store).resolve())

    def test_default_next_to_cookies_under_data_dir(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "TR_ADAPTER_DATA_DIR": tmp,
                "TR_COOKIES_FILE": "tr_cookies.txt",
            }
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("TR_MCP_CONFIRM_STORE", None)
                path = _confirm_store_path()
                self.assertEqual(
                    path,
                    Path(tmp).resolve() / "tr_cookies.txt.confirmations.json",
                )

    def test_relative_confirm_store_uses_data_dir(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "TR_ADAPTER_DATA_DIR": tmp,
                    "TR_MCP_CONFIRM_STORE": "local.confirmations.json",
                },
            ):
                path = _confirm_store_path()
                self.assertEqual(
                    path,
                    Path(tmp).resolve() / "local.confirmations.json",
                )


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

    def test_binding_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfirmationStore(Path(tmp) / "confirmations.json")
            binding = {"size": 1, "limit": 100.0, "order_type": "buy"}
            token = store.issue(
                "place_limit_order", "US0378331005", binding=binding
            )["confirm_token"]
            with self.assertRaises(ConfirmationError):
                store.redeem(
                    "place_limit_order",
                    "US0378331005",
                    token,
                    binding={"size": 2, "limit": 100.0, "order_type": "buy"},
                )

    def test_binding_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfirmationStore(Path(tmp) / "confirmations.json")
            binding = {"size": 1.5, "limit": 100.0, "order_type": "sell"}
            token = store.issue(
                "place_limit_order", "US0378331005", binding=binding
            )["confirm_token"]
            store.redeem(
                "place_limit_order",
                "US0378331005",
                token,
                binding={"size": 1.5, "limit": 100.0, "order_type": "sell"},
            )

    def test_normalize_binding_float(self):
        self.assertEqual(
            normalize_binding({"limit": 10.0, "size": 1}),
            {"limit": "10", "size": "1"},
        )

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

    def test_order_confirmation_message(self):
        msg = order_confirmation_message(
            action="place_stop_market_order",
            ticker="US0378331005",
            instrument_name="Apple",
            order_type="sell",
            size=2,
            stop=90,
            expiry="gtc",
            exchange="LSX",
        )
        self.assertIn("Stop-Loss", msg)
        self.assertIn("ECHTES GELD", msg)


if __name__ == "__main__":
    unittest.main()
