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


@unittest.skipUnless(
    os.environ.get("TR_LIVE_TESTS") == "1"
    and os.environ.get("TR_PHONE")
    and os.environ.get("TR_PIN"),
    "set TR_LIVE_TESTS=1 plus TR_PHONE and TR_PIN (and a device key) for live tests",
)
class LiveReadOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tr = TrBlockingApi(
            os.environ["TR_PHONE"],
            os.environ["TR_PIN"],
            locale=os.environ.get("TR_LOCALE", "de"),
            key_file=os.environ.get("TR_KEY_FILE", "key"),
        )

    def test_login_and_cash(self):
        self.tr.login()
        cash = self.tr.cash()
        self.assertIsNotNone(cash)


if __name__ == "__main__":
    unittest.main()
