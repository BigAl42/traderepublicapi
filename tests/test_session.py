"""Unit tests for auth circuit breaker and error classification."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "tr-adapter"
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

from session import (  # noqa: E402
    AuthCircuitBreaker,
    ClassifiedError,
    ErrorKind,
    SessionBlockedError,
    classify_auth_error,
)


class ClassifyAuthErrorTest(unittest.TestCase):
    def test_rate_limit(self):
        classified = classify_auth_error(Exception("Too many attempts"))
        self.assertEqual(classified.kind, ErrorKind.RATE_LIMITED)
        self.assertFalse(classified.retryable)
        self.assertFalse(classified.allow_relogin)

    def test_session_expired(self):
        classified = classify_auth_error(Exception("session expired 401"))
        self.assertEqual(classified.kind, ErrorKind.SESSION_EXPIRED)
        self.assertTrue(classified.allow_relogin)

    def test_login_timeout(self):
        classified = classify_auth_error(Exception("The login was not confirmed in time."))
        self.assertEqual(classified.kind, ErrorKind.AUTH_FAILED)


class AuthCircuitBreakerTest(unittest.TestCase):
    def test_opens_on_rate_limit_and_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "circuit.json"
            breaker = AuthCircuitBreaker(path, failure_threshold=5, cooldown_seconds=120)
            breaker.record_failure(
                ClassifiedError(
                    kind=ErrorKind.RATE_LIMITED,
                    message="too many",
                    retryable=False,
                    retry_after_seconds=120,
                )
            )
            self.assertTrue(breaker.is_open())
            self.assertGreater(breaker.remaining_cooldown_seconds(), 0)
            with self.assertRaises(SessionBlockedError):
                breaker.guard()

    def test_opens_after_threshold_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "circuit.json"
            breaker = AuthCircuitBreaker(path, failure_threshold=2, cooldown_seconds=60)
            failure = ClassifiedError(
                kind=ErrorKind.SESSION_EXPIRED,
                message="expired",
                retryable=True,
                allow_relogin=True,
            )
            breaker.record_failure(failure)
            self.assertFalse(breaker.is_open())
            breaker.record_failure(failure)
            self.assertTrue(breaker.is_open())

            reloaded = AuthCircuitBreaker(path, failure_threshold=2, cooldown_seconds=60)
            self.assertTrue(reloaded.is_open())

    def test_success_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "circuit.json"
            breaker = AuthCircuitBreaker(path, failure_threshold=1, cooldown_seconds=60)
            breaker.record_failure(
                ClassifiedError(
                    kind=ErrorKind.AUTH_FAILED,
                    message="fail",
                    retryable=True,
                )
            )
            self.assertTrue(breaker.is_open())
            # Force-close by rewriting open_until in the past via success after manual clear.
            breaker._state["open_until"] = time.time() - 1
            breaker._save()
            breaker.record_success()
            self.assertFalse(breaker.is_open())
            self.assertEqual(breaker._state["failure_count"], 0)


class WriteSessionPolicyTest(unittest.TestCase):
    def test_write_path_does_not_login_when_cold(self):
        import os
        from unittest.mock import MagicMock, patch

        with patch.dict(os.environ, {"TR_TOKEN": "tok", "TR_PHONE": "", "TR_PIN": ""}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                cookies = Path(tmp) / "cookies.txt"
                circuit = Path(tmp) / "circuit.json"
                with patch("tr_client.TRApi") as api_cls:
                    api = MagicMock()
                    api.cookies_file = cookies
                    api._resume_web_session.return_value = False
                    api_cls.return_value = api
                    with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                        from tr_client import TradeRepublicClient, TradeRepublicClientError

                        client = TradeRepublicClient(token="tok")
                        with self.assertRaises(TradeRepublicClientError) as ctx:
                            client._ensure_session_for_write()
                        self.assertEqual(ctx.exception.kind, ErrorKind.LOGIN_REQUIRED)
                        api.login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
