"""Tests for autonomous session renew helpers."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "tr-adapter"
ROOT = Path(__file__).resolve().parent.parent
for path in (str(ADAPTER_DIR), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from session import ErrorKind  # noqa: E402
from trapi.api import TRApi  # noqa: E402


class SessionExpiryHelpersTest(unittest.TestCase):
    def test_session_needs_refresh(self):
        api = TRApi("+49000000000", "0000", cookies_file="/tmp/no-cookies-auto-renew")
        api._session_expires_at = 0
        self.assertFalse(api.session_needs_refresh())
        api._session_expires_at = time.time() + 120
        self.assertFalse(api.session_needs_refresh(skew_seconds=45))
        api._session_expires_at = time.time() + 10
        self.assertTrue(api.session_needs_refresh(skew_seconds=45))

    def test_load_cookies_from_disk_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = TRApi(
                "+49000000000",
                "0000",
                cookies_file=str(Path(tmp) / "missing.txt"),
            )
            self.assertFalse(api.load_cookies_from_disk())


class AutoRenewClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_recover_session_soft_refresh(self):
        with patch.dict(
            os.environ,
            {"TR_MCP_AUTO_RENEW": "1", "TR_TOKEN": "tok", "TR_PHONE": "", "TR_PIN": ""},
            clear=False,
        ):
            with tempfile.TemporaryDirectory() as tmp:
                cookies = Path(tmp) / "c.txt"
                circuit = Path(tmp) / "circuit.json"
                with patch("tr_client.TRApi") as api_cls:
                    api = MagicMock()
                    api.cookies_file = cookies
                    api.session_needs_refresh.return_value = False
                    api.load_cookies_from_disk.return_value = True
                    api._refresh_web_session.return_value = MagicMock(status_code=200)
                    api.refresh_account_settings.return_value = {"ok": True}
                    api.reset_transport = AsyncMock()
                    api_cls.return_value = api
                    with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                        from tr_client import TradeRepublicClient

                        client = TradeRepublicClient(token="tok")
                        client._api = api
                        ok = await client._recover_session()
                        self.assertTrue(ok)
                        self.assertEqual(client._last_renew_result, "soft_refresh")
                        self.assertTrue(client._session_ready)

    async def test_query_auth_retries_with_factory_after_recover(self):
        from trapi.api import TRapiException
        from tr_client import TradeRepublicClient

        with patch.dict(os.environ, {"TR_MCP_AUTO_RENEW": "1"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                cookies = Path(tmp) / "c.txt"
                circuit = Path(tmp) / "circuit.json"
                with patch("tr_client.TRApi") as api_cls:
                    api = MagicMock()
                    api.cookies_file = cookies
                    api.session_needs_refresh.return_value = False
                    api.reset_transport = AsyncMock()
                    api_cls.return_value = api
                    with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                        client = TradeRepublicClient(token="tok")
                        client._api = api
                        client._session_ready = True
                        client._has_credentials = True

                        calls = {"n": 0}

                        def factory():
                            calls["n"] += 1

                            async def coro():
                                return {"ok": True}

                            return coro()

                        async def query_side_effect(coro):
                            await coro
                            if calls["n"] == 1:
                                raise TRapiException("401 unauthorized session expired")
                            return {"ok": True}

                        client._query = AsyncMock(side_effect=query_side_effect)
                        client._recover_session = AsyncMock(return_value=True)
                        client._throttle_read = AsyncMock()
                        client._ensure_session = AsyncMock()

                        result = await client._query_auth(factory)
                        self.assertEqual(result, {"ok": True})
                        self.assertEqual(calls["n"], 2)
                        client._recover_session.assert_awaited_once()

    async def test_auto_renew_disabled_skips_recover(self):
        from trapi.api import TRapiException
        from tr_client import TradeRepublicClient, TradeRepublicClientError

        with patch.dict(os.environ, {"TR_MCP_AUTO_RENEW": "0"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                cookies = Path(tmp) / "c.txt"
                circuit = Path(tmp) / "circuit.json"
                with patch("tr_client.TRApi") as api_cls:
                    api = MagicMock()
                    api.cookies_file = cookies
                    api.session_needs_refresh.return_value = False
                    api.reset_transport = AsyncMock()
                    api.reset_transport_sync = MagicMock()
                    api_cls.return_value = api
                    with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                        client = TradeRepublicClient(token="tok")
                        self.assertFalse(client._auto_renew)
                        client._api = api
                        client._session_ready = True
                        client._has_credentials = True

                        async def query_side_effect(coro):
                            await coro
                            raise TRapiException("401 unauthorized session expired")

                        client._query = AsyncMock(side_effect=query_side_effect)
                        client._throttle_read = AsyncMock()
                        client._ensure_session = AsyncMock()
                        soft = AsyncMock(return_value=True)
                        client._soft_refresh_session = soft

                        with self.assertRaises(TradeRepublicClientError) as ctx:
                            await client._query_auth(lambda: asyncio.sleep(0))
                        self.assertEqual(ctx.exception.kind, ErrorKind.LOGIN_REQUIRED)
                        soft.assert_not_awaited()
                        self.assertEqual(client._last_renew_result, "disabled")

    async def test_cold_session_guidance_forbids_provider_switch(self):
        from errors import error_payload
        from tr_client import TradeRepublicClientError

        exc = TradeRepublicClientError(
            "cold",
            retryable=True,
            kind=ErrorKind.LOGIN_REQUIRED,
            retry_after_seconds=30,
        )
        payload = error_payload(exc)
        self.assertEqual(payload["code"], "login_required")
        self.assertIn("renew_session", payload["guidance"])
        self.assertIn("Do NOT switch", payload["guidance"])
        self.assertIn("ClawStreet", payload["guidance"])

    async def test_renew_session_soft_refresh_ready(self):
        from tr_client import TradeRepublicClient

        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "c.txt"
            circuit = Path(tmp) / "circuit.json"
            with patch("tr_client.TRApi") as api_cls:
                api = MagicMock()
                api.cookies_file = cookies
                api._process_id = None
                api.reset_transport = AsyncMock()
                api_cls.return_value = api
                with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                    client = TradeRepublicClient(token="tok")
                    client._api = api
                    client._soft_refresh_session = AsyncMock(return_value=True)
                    client._last_renew_http_status = 200
                    result = await client.renew_session()
                    self.assertEqual(result["status"], "ready")
                    self.assertEqual(result["method"], "soft_refresh")

    async def test_renew_session_starts_push_when_soft_fails(self):
        from tr_client import TradeRepublicClient

        with patch.dict(
            os.environ,
            {
                "TR_MCP_RENEW_ALLOW_PUSH": "1",
                "TR_PHONE": "+491111111111",
                "TR_PIN": "1234",
            },
            clear=False,
        ):
            with tempfile.TemporaryDirectory() as tmp:
                cookies = Path(tmp) / "c.txt"
                circuit = Path(tmp) / "circuit.json"
                with patch("tr_client.TRApi") as api_cls:
                    api = MagicMock()
                    api.cookies_file = cookies
                    api._process_id = None
                    api.reset_transport = AsyncMock()
                    api.start_web_login.return_value = {
                        "process_id": "proc-1",
                        "status": "PENDING",
                        "required_action": None,
                        "expires_at": None,
                    }
                    api_cls.return_value = api
                    with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                        client = TradeRepublicClient(token="tok")
                        client._api = api
                        client._soft_refresh_session = AsyncMock(return_value=False)
                        client._try_resume = AsyncMock(return_value=False)
                        client._last_renew_http_status = 401
                        result = await client.renew_session()
                        self.assertEqual(result["status"], "awaiting_push_confirm")
                        self.assertEqual(result["process_id"], "proc-1")
                        self.assertIn("confirm", result["guidance"].lower())
                        api.start_web_login.assert_called_once()

    async def test_renew_session_finalizes_after_confirm(self):
        from tr_client import TradeRepublicClient

        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "c.txt"
            circuit = Path(tmp) / "circuit.json"
            with patch("tr_client.TRApi") as api_cls:
                api = MagicMock()
                api.cookies_file = cookies
                api._process_id = "proc-1"
                api.reset_transport = AsyncMock()
                api.poll_web_login.return_value = {
                    "process_id": "proc-1",
                    "status": "CONFIRMED",
                    "required_action": None,
                    "expires_at": None,
                }
                api.finalize_web_login.return_value = True
                api_cls.return_value = api
                with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                    client = TradeRepublicClient(token="tok")
                    client._api = api
                    client._login_process_id = "proc-1"
                    result = await client.renew_session()
                    self.assertEqual(result["status"], "ready")
                    self.assertEqual(result["method"], "push_login")
                    self.assertTrue(client._session_ready)


if __name__ == "__main__":
    unittest.main()
