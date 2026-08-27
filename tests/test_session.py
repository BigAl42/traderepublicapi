"""Unit tests for auth circuit breaker and error classification."""

from __future__ import annotations

import asyncio
import os
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
    adapter_dir,
    circuit_state_path_for_cookies,
    classify_auth_error,
    clear_login_process,
    load_login_process,
    login_process_path_for_cookies,
    resolve_runtime_path,
    save_login_process,
)


class ResolveRuntimePathTest(unittest.TestCase):
    def test_absolute_paths_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            absolute = Path(tmp) / "cookies.txt"
            self.assertEqual(resolve_runtime_path(absolute), absolute.resolve())

    def test_relative_paths_anchor_to_adapter_dir_by_default(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TR_ADAPTER_DATA_DIR", None)
            resolved = resolve_runtime_path("tr_cookies.txt")
            self.assertTrue(resolved.is_absolute())
            self.assertEqual(resolved.parent, adapter_dir())
            self.assertEqual(resolved.name, "tr_cookies.txt")

    def test_data_dir_override(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"TR_ADAPTER_DATA_DIR": tmp}):
                resolved = resolve_runtime_path("tr_cookies.txt")
                self.assertEqual(resolved, Path(tmp).resolve() / "tr_cookies.txt")

    def test_circuit_path_next_to_resolved_cookies(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"TR_ADAPTER_DATA_DIR": tmp}):
                circuit = circuit_state_path_for_cookies("tr_cookies.txt")
                self.assertEqual(
                    circuit,
                    Path(tmp).resolve() / "tr_cookies.txt.auth_circuit.json",
                )

    def test_login_process_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.txt.login_process.json"
            save_login_process(path, process_id="abc-123", expires_at=time.time() + 600)
            loaded = load_login_process(path)
            self.assertEqual(loaded["process_id"], "abc-123")
            clear_login_process(path)
            self.assertIsNone(load_login_process(path))

    def test_login_process_expired_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.txt.login_process.json"
            save_login_process(path, process_id="old", expires_at=time.time() - 10)
            self.assertIsNone(load_login_process(path))
            self.assertFalse(path.is_file())

    def test_login_process_path_next_to_cookies(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"TR_ADAPTER_DATA_DIR": tmp}):
                path = login_process_path_for_cookies("tr_cookies.txt")
                self.assertEqual(
                    path,
                    Path(tmp).resolve() / "tr_cookies.txt.login_process.json",
                )

    def test_circuit_lock_lives_next_to_absolute_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Change cwd so a relative state path would diverge without resolve_runtime_path.
            original = Path.cwd()
            other = Path(tmp) / "other_cwd"
            other.mkdir()
            state = Path(tmp) / "circuit.json"
            try:
                os.chdir(other)
                breaker = AuthCircuitBreaker(state, failure_threshold=1, cooldown_seconds=30)
                self.assertTrue(breaker.state_path.is_absolute())
                self.assertEqual(breaker.state_path, state.resolve())
                lock_path = Path(str(breaker.state_path) + ".lock")
                breaker.record_failure(
                    ClassifiedError(
                        kind=ErrorKind.AUTH_FAILED,
                        message="fail",
                        retryable=True,
                    )
                )
                self.assertTrue(lock_path.is_file())
                self.assertEqual(
                    os.path.realpath(lock_path.parent),
                    os.path.realpath(state.parent),
                )
            finally:
                os.chdir(original)


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
        from unittest.mock import AsyncMock, MagicMock, patch

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
                        client._soft_refresh_session = AsyncMock(return_value=False)
                        client._maybe_resume_inflight_push = AsyncMock()
                        client._recover_session = AsyncMock(return_value=False)
                        with self.assertRaises(TradeRepublicClientError) as ctx:
                            asyncio.run(client._ensure_session_for_write())
                        self.assertEqual(ctx.exception.kind, ErrorKind.SESSION_EXPIRED)
                        api.login.assert_not_called()

    def test_interactive_login_defaults_off(self):
        import os
        from unittest.mock import AsyncMock, MagicMock, patch

        env = {
            "TR_TOKEN": "",
            "TR_PHONE": "+491234",
            "TR_PIN": "1234",
        }
        # Ensure default env var is unset so client default applies.
        env.pop("TR_MCP_ALLOW_INTERACTIVE_LOGIN", None)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TR_MCP_ALLOW_INTERACTIVE_LOGIN", None)
            with tempfile.TemporaryDirectory() as tmp:
                cookies = Path(tmp) / "cookies.txt"
                circuit = Path(tmp) / "circuit.json"
                with patch("tr_client.TRApi") as api_cls:
                    api = MagicMock()
                    api.cookies_file = cookies
                    api._resume_web_session.return_value = False
                    api_cls.return_value = api
                    with patch("tr_client.circuit_state_path_for_cookies", return_value=circuit):
                        import importlib
                        import tr_client

                        importlib.reload(tr_client)
                        client = tr_client.TradeRepublicClient()
                        self.assertFalse(client._allow_interactive_login)
                        asyncio.run(client._ensure_session(allow_login=True))
                        api.login.assert_not_called()
                        self.assertFalse(client._session_ready)


if __name__ == "__main__":
    unittest.main()
