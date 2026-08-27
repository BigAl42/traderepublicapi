"""Tests for autonomous session renew helpers and public WS cookie poison handling."""

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
        self.assertIn("same MCP tool", payload["guidance"])
        self.assertIn("switch providers", payload["guidance"])
        self.assertIn("trigger_login.py", payload["guidance"])

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
                    client._login_process_path = Path(tmp) / "c.txt.login_process.json"
                    result = await client.renew_session()
                    self.assertEqual(result["status"], "ready")
                    self.assertEqual(result["method"], "push_login")
                    self.assertTrue(client._session_ready)
                    self.assertIsNone(client._login_process_id)
                    self.assertFalse(client._login_process_path.is_file())

    async def test_renew_persists_process_id_across_client_restart(self):
        from tr_client import TradeRepublicClient

        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "c.txt"
            circuit = Path(tmp) / "circuit.json"
            login_path = Path(tmp) / "c.txt.login_process.json"
            with patch("tr_client.TRApi") as api_cls:
                api = MagicMock()
                api.cookies_file = cookies
                api._process_id = None
                api.reset_transport = AsyncMock()
                api.start_web_login.return_value = {
                    "process_id": "proc-persist",
                    "status": "PENDING",
                    "required_action": None,
                    "expires_at": None,
                }
                api._soft_refresh = False
                api_cls.return_value = api
                with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                    with patch("tr_client.login_process_path_for_cookies", return_value=login_path):
                        client = TradeRepublicClient(token="tok")
                        client._api = api
                        client._auto_renew = True
                        client._renew_allow_push = True
                        with patch.dict(
                            "os.environ",
                            {"TR_PHONE": "+49123", "TR_PIN": "1234"},
                            clear=False,
                        ):
                            with patch.object(
                                client, "_soft_refresh_session", new=AsyncMock(return_value=False)
                            ):
                                with patch.object(
                                    client, "_try_resume", new=AsyncMock(return_value=False)
                                ):
                                    first = await client.renew_session()
                        self.assertEqual(first["status"], "awaiting_push_confirm")
                        self.assertTrue(login_path.is_file())

                        # Simulate MCP respawn: new client, same disk sidecar.
                        api2 = MagicMock()
                        api2.cookies_file = cookies
                        api2._process_id = None
                        api2.reset_transport = AsyncMock()
                        api2.poll_web_login.return_value = {
                            "process_id": "proc-persist",
                            "status": "PENDING",
                            "required_action": None,
                            "expires_at": None,
                        }
                        api_cls.return_value = api2
                        client2 = TradeRepublicClient(token="tok")
                        client2._api = api2
                        client2._login_process_path = login_path
                        client2._restore_login_process()
                        self.assertEqual(client2._login_process_id, "proc-persist")
                        second = await client2.renew_session()
                        self.assertEqual(second["status"], "awaiting_push_confirm")
                        api2.start_web_login.assert_not_called()
                        api2.poll_web_login.assert_called()


class PublicWsDeadCookieTest(unittest.IsolatedAsyncioTestCase):
    def test_clear_tr_session_cookie(self):
        from http.cookiejar import Cookie

        api = TRApi("+49000000000", "0000", cookies_file="/tmp/no-cookies-public-ws")
        cookie = Cookie(
            version=0,
            name="tr_session",
            value="dead",
            port=None,
            port_specified=False,
            domain=".traderepublic.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": ""},
        )
        api.session.cookies.set_cookie(cookie)
        api.sessionToken = "dead"
        api._session_expires_at = time.time() + 100
        self.assertTrue(api._has_tr_session_cookie())
        api.clear_tr_session_cookie()
        self.assertFalse(api._has_tr_session_cookie())
        self.assertIsNone(api.sessionToken)
        self.assertEqual(api._session_expires_at, 0)

    async def test_query_public_retries_anonymously_after_401(self):
        from trapi.api import TRapiException
        from tr_client import TradeRepublicClient

        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "c.txt"
            circuit = Path(tmp) / "circuit.json"
            with patch("tr_client.TRApi") as api_cls:
                api = MagicMock()
                api.cookies_file = cookies
                api.sessionToken = "dead"
                api._has_tr_session_cookie.return_value = True
                api.clear_tr_session_cookie = MagicMock()
                api.reset_transport = AsyncMock()
                api_cls.return_value = api
                with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                    client = TradeRepublicClient(token="tok")
                    client._api = api
                    calls = {"n": 0}

                    def factory():
                        calls["n"] += 1

                        async def coro():
                            return {"bars": [1]}

                        return coro()

                    async def query_side_effect(coro):
                        await coro
                        if calls["n"] == 1:
                            raise TRapiException("Connection Error: HTTP 401 Unauthorized")
                        return {"bars": [1]}

                    client._query = AsyncMock(side_effect=query_side_effect)
                    result = await client._query_public(factory)
                    self.assertEqual(result, {"bars": [1]})
                    self.assertEqual(calls["n"], 2)
                    api.clear_tr_session_cookie.assert_called_once()
                    self.assertFalse(client._session_ready)


if __name__ == "__main__":
    unittest.main()
