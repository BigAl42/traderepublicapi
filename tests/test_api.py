import asyncio
import os
import unittest

from trapi.api import TRApi, TrBlockingApi, TRapiException


class DecodeUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.api = TRApi("+49000000000", "0000")

    def test_keep_skip_and_insert(self):
        self.api.latest_response["1"] = "abcdefghij"
        decoded = self.api.decode_updates("1", ["=3", "-2", "+XY", "=5"])
        self.assertEqual(decoded, "abcXYfghij")

    def test_unknown_instruction_raises(self):
        self.api.latest_response["1"] = "abc"
        with self.assertRaises(TRapiException):
            self.api.decode_updates("1", ["?1"])


class ValidationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = TRApi("+49000000000", "0000")

    async def test_invalid_history_range(self):
        with self.assertRaises(TRapiException):
            await self.api.aggregate_history_light("US0000000000", range="nope")

    async def test_invalid_exchange(self):
        with self.assertRaises(TRapiException):
            await self.api.ticker("US0000000000", exchange="XXX")

    async def test_invalid_product_category(self):
        with self.assertRaises(TRapiException):
            await self.api.derivatives("US0000000000", "unknown")


class SurfaceTest(unittest.TestCase):
    def test_type_to_id_defaults(self):
        api = TRApi("+49000000000", "0000")
        self.assertEqual(api.type_to_id("cash"), "0")
        self.assertEqual(api.type_to_id("portfolio"), "1")
        self.assertEqual(api.type_to_id("availableCash"), "2")
        self.assertIsNone(api.type_to_id("unknown"))

    def test_key_file_env_and_arg(self):
        api = TRApi("+49000000000", "0000", key_file="/tmp/tr.key")
        self.assertEqual(api.key_file, "/tmp/tr.key")

    def test_default_auth_is_web(self):
        from trapi.api import WS_CONNECT_ID_WEB

        api = TRApi("+49000000000", "0000")
        self.assertEqual(api.auth, "web")
        self.assertEqual(WS_CONNECT_ID_WEB, 31)
        headers = api._login_headers()
        for name in ("X-TR-Device-Info", "X-TR-App-Version", "X-Tr-Platform", "Accept-Language"):
            self.assertIn(name, headers)

    def test_blocking_client_exposes_read_helpers(self):
        names = [
            "cash",
            "portfolio",
            "account_pairs",
            "compact_portfolio_by_type",
            "savings_plans",
            "timeline_detail_v2",
            "timeline_transactions",
            "watchlist",
        ]
        for name in names:
            self.assertTrue(callable(getattr(TrBlockingApi, name)), name)

    def test_public_exports(self):
        import trapi

        self.assertTrue(hasattr(trapi, "TRApi"))
        self.assertTrue(hasattr(trapi, "TrBlockingApi"))
        self.assertTrue(hasattr(trapi, "TRapiException"))


class TransportLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_receive_one_clears_started_on_error(self):
        api = TRApi("+49000000000", "0000")

        async def boom():
            raise TRapiException("simulated ws failure")

        api.get_data = boom  # type: ignore[method-assign]
        with self.assertRaises(TRapiException):
            await api.start(receive_one=True)
        self.assertFalse(api.started)
        # Can start again after failure cleanup.
        with self.assertRaises(TRapiException):
            await api.start(receive_one=True)
        self.assertFalse(api.started)

    async def test_reset_transport_allows_reconnect(self):
        api = TRApi("+49000000000", "0000")
        api.started = True
        api.ws = object()
        await api.reset_transport()
        self.assertFalse(api.started)
        self.assertIsNone(api.ws)


class ResumeWebSessionTest(unittest.TestCase):
    def test_resume_without_cookie_file_uses_in_memory_session(self):
        from http.cookiejar import Cookie
        from unittest.mock import MagicMock

        api = TRApi("+49000000000", "0000", cookies_file="/tmp/does-not-exist-tr-cookies.txt")
        if api.cookies_file.is_file():
            api.cookies_file.unlink()
        cookie = Cookie(
            version=0,
            name="tr_session",
            value="token-value",
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
        api.refresh_account_settings = MagicMock(return_value={"securitiesAccountNumber": "123"})
        self.assertTrue(api._resume_web_session())
        self.assertEqual(api.sessionToken, "token-value")
        api.refresh_account_settings.assert_called_once()


class CurrentWebProtocolTest(unittest.TestCase):
    def test_v2_login_endpoint_accepts_headers(self):
        api = TRApi("+49000000000", "0000")
        response = api.session.post(
            f"{api.url}/api/v2/auth/web/login",
            json={"phoneNumber": api.number, "pin": api.pin},
            headers=api._login_headers(),
            timeout=20,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("NUMBER_INVALID", response.text)


class PublicSocketTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_tags_over_connect_31(self):
        api = TRApi("+49000000000", "0000")
        await api.neon_search_tags()
        payload = await asyncio.wait_for(api.start(receive_one=True), timeout=20)
        self.assertIn("tags", payload)
        self.assertGreater(len(payload["tags"]), 0)


@unittest.skipUnless(
    os.environ.get("TR_LIVE_TESTS") == "1"
    and os.environ.get("TR_PHONE")
    and os.environ.get("TR_PIN"),
    "set TR_LIVE_TESTS=1 plus TR_PHONE and TR_PIN; confirm the app push",
)
class LiveReadOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tr = TrBlockingApi(
            os.environ["TR_PHONE"],
            os.environ["TR_PIN"],
            locale=os.environ.get("TR_LOCALE", "de"),
        )

    def test_login_and_cash(self):
        self.tr.login()
        cash = self.tr.cash()
        self.assertIsNotNone(cash)


if __name__ == "__main__":
    unittest.main()
