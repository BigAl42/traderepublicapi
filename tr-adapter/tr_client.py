"""Trade Republic client wrapper for the MCP adapter.

Uses TRApi (async) directly so all data methods can be awaited inside the
MCP server's already-running event loop — avoids the ``RuntimeError: This
event loop is already running`` that TrBlockingApi triggers via
``run_until_complete``.

Session policy (professional):
- Cookie / TR_TOKEN first, push-login last
- File-backed auth circuit breaker across Hermes process restarts
- Soft session refresh before mutating calls
- No login storms on rate-limit errors

Credentials come from environment variables only (see .env.example).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trapi.api import TRApi, TRapiException, TRapiExcServerErrorState  # noqa: E402

from redact import redact_secrets  # noqa: E402
from session import (  # noqa: E402
    AuthCircuitBreaker,
    ClassifiedError,
    ErrorKind,
    SessionBlockedError,
    circuit_state_path_for_cookies,
    classify_auth_error,
    clear_login_process,
    load_login_process,
    login_process_path_for_cookies,
    resolve_runtime_path,
    save_login_process,
)

LOGGER = logging.getLogger("tr_adapter.client")

_DEFAULT_VERIFY_BACKOFF_SEC = 60


class TradeRepublicClientError(Exception):
    """Human-readable adapter error for MCP tools."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        kind: ErrorKind = ErrorKind.UNKNOWN,
        retry_after_seconds: int | None = None,
        guidance: str | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds
        self.guidance = guidance

    @classmethod
    def from_classified(cls, classified: ClassifiedError) -> TradeRepublicClientError:
        return cls(
            classified.message,
            retryable=classified.retryable,
            kind=classified.kind,
            retry_after_seconds=classified.retry_after_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "code": getattr(self.kind, "value", None) or ErrorKind.UNKNOWN.value,
            "message": redact_secrets(str(self)),
            "retryable": bool(self.retryable),
            "retry_after_seconds": self.retry_after_seconds,
        }


class TradeRepublicClient:
    """Async facade over TRApi for Hermes / MCP with hardened session handling."""

    def __init__(self, token: str | None = None):
        self._token = token or os.getenv("TR_TOKEN")
        phone = os.getenv("TR_PHONE", "")
        pin = os.getenv("TR_PIN", "")
        locale = os.getenv("TR_LOCALE", "de")
        # Absolute path so cookies / circuit / confirmations stay stable when Hermes
        # respawns the MCP process with a different working directory.
        cookies_file = str(
            resolve_runtime_path(os.getenv("TR_COOKIES_FILE", "tr_cookies.txt"))
        )
        self._timeout = float(os.getenv("TR_TIMEOUT", "20"))
        self._has_credentials = bool(self._token or (phone and pin))
        self._allow_interactive_login = os.getenv(
            "TR_MCP_ALLOW_INTERACTIVE_LOGIN", "0"
        ).strip().lower() in ("1", "true", "yes")
        # Soft cookie/token renew on cold/401. Default ON — no push login.
        self._auto_renew = os.getenv("TR_MCP_AUTO_RENEW", "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self._last_renew_at: float | None = None
        self._last_renew_result: str | None = None
        self._last_renew_http_status: int | None = None
        self._login_process_id: str | None = None
        # Auto-recover may start push login when cookies are dead (TR_MCP_RENEW_ALLOW_PUSH).
        self._renew_allow_push = os.getenv(
            "TR_MCP_RENEW_ALLOW_PUSH", "1"
        ).strip().lower() in ("1", "true", "yes")

        self._api = TRApi(
            phone or "+0000000000",
            pin or "0000",
            locale=locale,
            cookies_file=cookies_file,
            auth="web",
        )
        self._session_ready = False
        self._circuit = AuthCircuitBreaker(
            circuit_state_path_for_cookies(self._api.cookies_file)
        )
        self._login_process_path = login_process_path_for_cookies(self._api.cookies_file)
        self._restore_login_process()
        self._last_read_at = 0.0
        self._read_lock = asyncio.Lock()
        self._last_uncertain_write_at: float | None = None
        self._last_uncertain_write_action: str | None = None
        try:
            self._write_verify_backoff_sec = max(
                0,
                int(
                    os.getenv(
                        "TR_MCP_WRITE_VERIFY_BACKOFF_SEC",
                        str(_DEFAULT_VERIFY_BACKOFF_SEC),
                    )
                    or str(_DEFAULT_VERIFY_BACKOFF_SEC)
                ),
            )
        except ValueError:
            self._write_verify_backoff_sec = _DEFAULT_VERIFY_BACKOFF_SEC

    def _restore_login_process(self) -> None:
        """Reload in-flight push login process_id after MCP respawn."""
        stored = load_login_process(self._login_process_path)
        if not stored:
            return
        self._login_process_id = stored["process_id"]
        self._api._process_id = stored["process_id"]
        LOGGER.info(
            "Restored in-flight TR login process_id from disk (will poll, not restart)"
        )

    def _remember_login_process(
        self, process_id: str | None, *, expires_at: Any = None
    ) -> None:
        if not process_id:
            return
        self._login_process_id = process_id
        self._api._process_id = process_id
        save_login_process(
            self._login_process_path,
            process_id=process_id,
            expires_at=expires_at,
        )

    def _forget_login_process(self) -> None:
        self._login_process_id = None
        if getattr(self._api, "_process_id", None):
            self._api._process_id = None
        clear_login_process(self._login_process_path)

    def _inject_token_on_api(self) -> None:
        if not self._token:
            return
        cookie = Cookie(
            version=0,
            name="tr_session",
            value=self._token,
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
        self._api.session.cookies.set_cookie(cookie)
        self._api.sessionToken = self._token

    def _persist_cookies(self) -> None:
        try:
            self._api._save_cookies()
        except Exception as exc:  # noqa: BLE001 — persistence must never crash callers
            LOGGER.warning("Could not persist TR cookies: %s", exc)

    def _invalidate_session(self) -> None:
        self._session_ready = False
        # Drop sticky websocket so the next query reconnects with fresh cookies.
        # Sync path only — never schedules tasks on FastMCP's running loop.
        try:
            self._api.reset_transport_sync()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Transport reset during invalidate failed: %s", exc)

    async def _invalidate_session_async(self) -> None:
        self._session_ready = False
        await self._reset_transport()

    async def _reset_transport(self) -> None:
        try:
            await self._api.reset_transport()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Async transport reset failed: %s", exc)
            self._api.reset_transport_sync()

    def _map_error(self, exc: Exception, *, record: bool = True) -> TradeRepublicClientError:
        if isinstance(exc, SessionBlockedError):
            return TradeRepublicClientError(
                str(exc),
                retryable=False,
                kind=exc.kind,
                retry_after_seconds=exc.retry_after_seconds,
            )
        if isinstance(exc, TradeRepublicClientError):
            return exc
        classified = classify_auth_error(exc)
        if record and classified.kind in {
            ErrorKind.RATE_LIMITED,
            ErrorKind.AUTH_FAILED,
            ErrorKind.SESSION_EXPIRED,
        }:
            self._circuit.record_failure(classified)
            self._invalidate_session()
        return TradeRepublicClientError.from_classified(classified)

    async def _try_resume(self) -> bool:
        """Cookie/token resume only — never starts a push login."""
        # Prefer latest disk cookies (may have been warmed by check_login offline).
        self._api.load_cookies_from_disk()
        # Re-read TR_TOKEN in case the environment was updated without respawn.
        env_token = os.getenv("TR_TOKEN", "").strip()
        if env_token:
            self._token = env_token
        if self._token:
            self._inject_token_on_api()
        if self._api._resume_web_session():
            self._persist_cookies()
            self._session_ready = True
            self._circuit.record_success()
            self._last_renew_at = time.time()
            self._last_renew_result = "resumed"
            # New HTTP session cookies must not reuse a stale websocket.
            await self._reset_transport()
            LOGGER.info("Resumed Trade Republic session from cookies/token")
            return True
        return False

    def _interactive_login(self) -> None:
        if not self._allow_interactive_login:
            raise TradeRepublicClientError(
                "Interactive login is disabled (TR_MCP_ALLOW_INTERACTIVE_LOGIN=0). "
                "Provide a valid TR_TOKEN or warm tr_cookies.txt offline via check_login.py.",
                retryable=False,
                kind=ErrorKind.LOGIN_REQUIRED,
            )
        if not os.getenv("TR_PHONE") or not os.getenv("TR_PIN"):
            raise TradeRepublicClientError(
                "Session expired or invalid TR_TOKEN. Set TR_PHONE and TR_PIN, "
                "then confirm the app push on login — or wait and reuse cookies.",
                retryable=True,
                kind=ErrorKind.LOGIN_REQUIRED,
            )
        LOGGER.info("Starting interactive Trade Republic web login (push confirm)")
        self._api.login(resume=False)
        self._persist_cookies()
        self._session_ready = True
        self._circuit.record_success()
        self._last_renew_at = time.time()
        self._last_renew_result = "interactive_login"

    async def _recover_session(self) -> bool:
        """Best-effort autonomous renew after 401 / cold session.

        Order: soft refresh → disk cookies + TR_TOKEN resume → optional push login
        (non-blocking, same process_id). Returns True when session is ready.
        Raises TradeRepublicClientError for awaiting_push_confirm / awaiting_authenticator.
        """
        if not self._auto_renew:
            self._last_renew_result = "disabled"
            self._last_renew_at = time.time()
            return False
        if self._circuit.is_open():
            self._last_renew_result = "circuit_open"
            self._last_renew_at = time.time()
            return False

        await self._invalidate_session_async()

        if await self._soft_refresh_session():
            self._last_renew_result = "soft_refresh"
            self._last_renew_at = time.time()
            LOGGER.info("Session recovered via soft refresh")
            return True

        if await self._try_resume():
            LOGGER.info("Session recovered via cookie/token resume")
            return True

        payload = await self._renew_session_internal()
        return self._apply_renew_payload(payload)

    def _apply_renew_payload(self, payload: dict[str, Any]) -> bool:
        """Interpret renew payload: True if ready; raise if user action needed."""
        status = payload.get("status")
        if status == "ready":
            return True
        if status == "awaiting_push_confirm":
            raise TradeRepublicClientError(
                payload.get("message")
                or (
                    "Trade Republic login push sent. Confirm it in the mobile app, "
                    "then call this same tool again."
                ),
                retryable=True,
                kind=ErrorKind.AWAITING_PUSH_CONFIRM,
                retry_after_seconds=15,
                guidance=payload.get("guidance"),
            )
        if status == "awaiting_authenticator":
            raise TradeRepublicClientError(
                payload.get("message")
                or "Trade Republic requires an authenticator code from the user.",
                retryable=True,
                kind=ErrorKind.AWAITING_AUTHENTICATOR,
                retry_after_seconds=None,
                guidance=payload.get("guidance"),
            )
        return False

    async def _ensure_session(self, *, allow_login: bool = True) -> None:
        """Ensure a usable web session without blocking push login.

        Cookie-first soft refresh/resume only. Push login is handled transparently
        on auth failure via ``_recover_session`` inside ``_query_auth``.
        """
        if self._session_ready:
            if self._auto_renew and self._api.session_needs_refresh():
                if await self._soft_refresh_session():
                    return
                self._session_ready = False
            else:
                return

        phone = bool(os.getenv("TR_PHONE", "").strip())
        pin = bool(os.getenv("TR_PIN", "").strip())
        self._has_credentials = bool(
            self._token or os.getenv("TR_TOKEN", "").strip() or (phone and pin)
        )
        if not self._has_credentials and not self._api.cookies_file.is_file():
            raise TradeRepublicClientError(
                "Missing credentials. Set TR_TOKEN (session) or TR_PHONE and TR_PIN in the environment, "
                "or place a warm cookie file at TR_COOKIES_FILE.",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )

        self._circuit.guard()

        try:
            if await self._try_resume():
                return
            if not allow_login:
                raise TradeRepublicClientError(
                    "No warm Trade Republic session available for this action. "
                    "Use a read tool first so the adapter can renew the session automatically.",
                    retryable=True,
                    kind=ErrorKind.LOGIN_REQUIRED,
                )
            # Cold session: proceed; _query_auth will recover on 401 or poll in-flight push.
        except SessionBlockedError as exc:
            raise self._map_error(exc) from exc
        except TRapiException as exc:
            raise self._map_error(exc) from exc

    async def _maybe_resume_inflight_push(self) -> None:
        """If a push login is in progress, poll once before the account query."""
        if not (self._api._process_id or self._login_process_id):
            self._restore_login_process()
        if not (self._api._process_id or self._login_process_id):
            return
        payload = await self._continue_push_login()
        self._apply_renew_payload(payload)

    async def _renew_session_internal(
        self,
        *,
        authenticator_code: str | None = None,
        allow_push: bool | None = None,
    ) -> dict[str, Any]:
        """Internal session renew: soft refresh → resume → push poll/start."""
        allow_push = self._renew_allow_push if allow_push is None else bool(allow_push)
        phone = bool(os.getenv("TR_PHONE", "").strip())
        pin = bool(os.getenv("TR_PIN", "").strip())

        try:
            self._circuit.guard()
        except SessionBlockedError as exc:
            raise self._map_error(exc) from exc

        if not (self._api._process_id or self._login_process_id):
            self._restore_login_process()
        if self._api._process_id or self._login_process_id:
            return await self._continue_push_login(authenticator_code=authenticator_code)

        if await self._soft_refresh_session():
            return {
                "status": "ready",
                "method": "soft_refresh",
                "session_ready": True,
                "http_status": self._last_renew_http_status,
            }

        if await self._try_resume():
            return {
                "status": "ready",
                "method": "resumed",
                "session_ready": True,
                "http_status": self._last_renew_http_status,
            }

        if not allow_push:
            self._last_renew_result = "push_disabled"
            self._last_renew_at = time.time()
            return {
                "status": "failed",
                "method": None,
                "session_ready": False,
                "code": "login_required",
                "http_status": self._last_renew_http_status,
                "message": (
                    "Soft renew failed and push login is disabled (TR_MCP_RENEW_ALLOW_PUSH=0)."
                ),
            }

        if not (phone and pin):
            self._last_renew_result = "missing_phone_pin"
            self._last_renew_at = time.time()
            return {
                "status": "failed",
                "method": None,
                "session_ready": False,
                "code": "login_required",
                "http_status": self._last_renew_http_status,
                "message": (
                    "Cookies/TR_TOKEN are cold. Operator must set TR_PHONE and TR_PIN "
                    "or provide a fresh TR_TOKEN."
                ),
            }

        try:
            started = await asyncio.to_thread(self._api.start_web_login)
        except (TRapiException, SessionBlockedError) as exc:
            self._last_renew_result = "push_start_failed"
            self._last_renew_at = time.time()
            raise self._map_error(exc) from exc

        self._remember_login_process(
            started.get("process_id"), expires_at=started.get("expires_at")
        )
        self._last_renew_result = "awaiting_push_confirm"
        self._last_renew_at = time.time()
        return self._push_login_status_payload(started)

    async def renew_session(
        self,
        *,
        authenticator_code: str | None = None,
        allow_push: bool | None = None,
    ) -> dict[str, Any]:
        """Operator/debug session renew (not exposed as an MCP tool)."""
        return await self._renew_session_internal(
            authenticator_code=authenticator_code,
            allow_push=allow_push,
        )

    async def _soft_refresh_session(self) -> bool:
        """Refresh web session endpoint without triggering login."""
        try:
            # Reload disk cookies first — may be fresher than in-memory jar.
            self._api.load_cookies_from_disk()
            response = self._api._refresh_web_session()
            status = int(getattr(response, "status_code", 500) or 500)
            self._last_renew_http_status = status
            if status >= 400:
                # Dead cookies poison public WebSocket connects — drop in-memory session.
                self._api.clear_tr_session_cookie()
                return False
            data = self._api.refresh_account_settings()
            if data is None:
                self._api.clear_tr_session_cookie()
                return False
            self._persist_cookies()
            self._session_ready = True
            self._circuit.record_success()
            self._last_renew_at = time.time()
            self._last_renew_result = "soft_refresh"
            # Force websocket reconnect with refreshed cookies.
            await self._reset_transport()
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Soft session refresh failed: %s", redact_secrets(str(exc)))
            return False

    async def _continue_push_login(
        self, *, authenticator_code: str | None = None
    ) -> dict[str, Any]:
        if self._login_process_id and not self._api._process_id:
            self._api._process_id = self._login_process_id

        try:
            if authenticator_code:
                polled = await asyncio.to_thread(
                    self._api.complete_web_login_authenticator, authenticator_code
                )
            else:
                polled = await asyncio.to_thread(self._api.poll_web_login)
        except (TRapiException, SessionBlockedError) as exc:
            self._last_renew_result = "push_poll_failed"
            self._last_renew_at = time.time()
            raise self._map_error(exc) from exc

        status = (polled.get("status") or "").upper()
        required = polled.get("required_action")

        if required == "AUTHENTICATOR_VERIFICATION" and not authenticator_code:
            self._last_renew_result = "awaiting_authenticator"
            self._last_renew_at = time.time()
            self._remember_login_process(
                polled.get("process_id") or self._login_process_id,
                expires_at=polled.get("expires_at"),
            )
            return {
                "status": "awaiting_authenticator",
                "method": "push_login",
                "session_ready": False,
                "process_id": polled.get("process_id"),
                "login_status": status,
                "required_action": required,
                "guidance": (
                    "Trade Republic requires an authenticator code. Ask the user for "
                    "the code, then retry this same MCP tool after they provide it. "
                    "Do not start a new login and do not write Python scripts."
                ),
            }

        if status in {"CONFIRMED", "COMPLETED"}:
            ok = await asyncio.to_thread(self._api.finalize_web_login)
            if not ok:
                self._last_renew_result = "finalize_failed"
                self._last_renew_at = time.time()
                self._forget_login_process()
                return {
                    "status": "failed",
                    "method": "push_login",
                    "session_ready": False,
                    "code": "login_required",
                    "guidance": (
                        "Push was confirmed but cookies were not established. "
                        "Retry this same MCP tool once. Do NOT run custom login scripts."
                    ),
                }
            self._persist_cookies()
            self._session_ready = True
            self._circuit.record_success()
            self._forget_login_process()
            self._last_renew_result = "push_login"
            self._last_renew_at = time.time()
            await self._reset_transport()
            return {
                "status": "ready",
                "method": "push_login",
                "session_ready": True,
                "guidance": (
                    "Session warmed after app confirmation. Retry the original "
                    "account/portfolio tool once — do not switch providers or run "
                    "login scripts."
                ),
            }

        if status in {None, "", "PENDING"}:
            self._last_renew_result = "awaiting_push_confirm"
            self._last_renew_at = time.time()
            self._remember_login_process(
                polled.get("process_id") or self._login_process_id,
                expires_at=polled.get("expires_at"),
            )
            return self._push_login_status_payload(polled)

        self._last_renew_result = f"push_status_{status.lower()}"
        self._last_renew_at = time.time()
        self._forget_login_process()
        return {
            "status": "failed",
            "method": "push_login",
            "session_ready": False,
            "code": "login_required",
            "login_status": status,
            "guidance": (
                f"Login process ended with status {status!r}. Ask the user to confirm "
                "readiness, then retry the same MCP tool. Never invent trigger_login.py "
                "or run check_login.py from the agent."
            ),
        }

    def _push_login_status_payload(self, started: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "awaiting_push_confirm",
            "method": "push_login",
            "session_ready": False,
            "process_id": started.get("process_id") or self._login_process_id,
            "login_status": started.get("status"),
            "required_action": started.get("required_action"),
            "expires_at": started.get("expires_at"),
            "guidance": (
                "ONE push was already sent. Ask the user to confirm it in the Trade "
                "Republic app, then call this SAME MCP tool again to poll and finalize. "
                "Do NOT start a new login, do NOT run check_login.py / trigger_login.py / "
                "tr.login() / any custom Python, and do NOT switch providers."
            ),
        }

    async def _ensure_session_for_write(self) -> None:
        """Warm session for mutating calls — resume, soft refresh, transparent push."""
        self._circuit.guard()
        if self._session_ready:
            if await self._soft_refresh_session():
                return
            await self._invalidate_session_async()

        await self._ensure_session(allow_login=True)
        if await self._soft_refresh_session():
            return
        await self._maybe_resume_inflight_push()
        if not await self._recover_session():
            raise TradeRepublicClientError(
                "Session is not warm enough for mutating Trade Republic actions. "
                "Confirm the Trade Republic app push if one was sent, then retry "
                "this same tool.",
                retryable=True,
                kind=ErrorKind.SESSION_EXPIRED,
                retry_after_seconds=self._circuit.remaining_cooldown_seconds() or 60,
            )

    def get_adapter_status(self) -> dict[str, Any]:
        """Local adapter health — no Trade Republic network call."""
        from mcp_write import trading_enabled, write_enabled

        cookies_path = Path(self._api.cookies_file)
        cooldown = self._circuit.remaining_cooldown_seconds()
        phone = bool(os.getenv("TR_PHONE", "").strip())
        pin = bool(os.getenv("TR_PIN", "").strip())

        uncertain_remaining = 0
        if self._last_uncertain_write_at is not None:
            elapsed = time.monotonic() - self._last_uncertain_write_at
            uncertain_remaining = max(
                0, int(self._write_verify_backoff_sec - elapsed)
            )

        if cooldown > 0:
            guidance = (
                "Auth circuit open — do not login or mutate. Wait "
                f"{cooldown}s, then call get_adapter_status again."
            )
            overall = "cooldown"
        elif uncertain_remaining > 0:
            guidance = (
                "Last mutation was unverified. Wait "
                f"{uncertain_remaining}s before another mutation; "
                "prefer get_watchlist / list_open_orders to check state."
            )
            overall = "write_backoff"
        elif self._login_process_id or getattr(self._api, "_process_id", None):
            guidance = (
                "Push login already in progress (login_process_id set). "
                "Ask the user to confirm in the Trade Republic app, then retry the "
                "same MCP tool to poll — do NOT start a new login and do NOT "
                "run Python login scripts."
            )
            overall = "awaiting_push_confirm"
        elif self._session_ready:
            guidance = "Session marked ready in-process. Prefer reads; mutate only with confirm_token."
            overall = "ready"
        elif cookies_path.is_file() or bool(self._token):
            guidance = (
                "Credentials/cookies present but session not warm in this process yet. "
                "Authenticated tools auto-renew on use; if a push was sent, confirm it "
                "in the app and retry the same tool. Public tools (charts/search) retry "
                "anonymously if cookies are dead. Do not switch data providers or "
                "invent login scripts."
            )
            overall = "cold"
        else:
            guidance = (
                "No warm session material. Retry an authenticated MCP tool — the adapter "
                "will renew automatically. Do not run check_login.py / trigger_login.py / "
                "custom Python from the agent."
            )
            overall = "unconfigured"

        return {
            "status": overall,
            "session_ready": self._session_ready,
            "has_token_env": bool(self._token),
            "has_phone_pin_env": phone and pin,
            "cookies_file": str(cookies_path),
            "cookies_file_exists": cookies_path.is_file(),
            "allow_interactive_login": self._allow_interactive_login,
            "auto_renew": self._auto_renew,
            "renew_allow_push": self._renew_allow_push,
            "last_renew_at": self._last_renew_at,
            "last_renew_result": self._last_renew_result,
            "last_renew_http_status": self._last_renew_http_status,
            "login_process_id": self._login_process_id or getattr(self._api, "_process_id", None),
            "session_expires_at": getattr(self._api, "_session_expires_at", None) or None,
            "write_enabled": write_enabled(),
            "trading_enabled": trading_enabled(),
            "auth_circuit_open": cooldown > 0,
            "auth_cooldown_remaining_seconds": cooldown,
            "retry_after_seconds": max(cooldown, uncertain_remaining) or None,
            "write_verify_backoff_remaining_seconds": uncertain_remaining,
            "last_uncertain_write_action": self._last_uncertain_write_action,
            "guidance": guidance,
        }

    def _note_uncertain_write(self, action: str) -> dict[str, Any]:
        self._last_uncertain_write_at = time.monotonic()
        self._last_uncertain_write_action = action
        check = (
            "list_open_orders"
            if action in {
                "place_limit_order",
                "place_stop_market_order",
                "cancel_order",
            }
            else "get_watchlist"
        )
        return {
            "retry_after_seconds": self._write_verify_backoff_sec,
            "guidance": (
                f"Do not retry {action} immediately. Call {check} and/or "
                "get_adapter_status, wait at least retry_after_seconds, then decide. "
                "Avoid login storms during cooldown."
            ),
        }

    def _guard_write_backoff(self, action: str) -> None:
        if self._last_uncertain_write_at is None:
            return
        elapsed = time.monotonic() - self._last_uncertain_write_at
        remaining = int(self._write_verify_backoff_sec - elapsed)
        if remaining <= 0:
            return
        raise TradeRepublicClientError(
            (
                f"Write backoff active after unverified '{self._last_uncertain_write_action}'. "
                f"Wait ~{remaining}s before '{action}'. "
                "Check get_watchlist / list_open_orders / get_adapter_status."
            ),
            retryable=True,
            kind=ErrorKind.SERVER,
            retry_after_seconds=remaining,
        )

    async def _throttle_read(self) -> None:
        """Light spacing between authenticated reads to reduce reconnect storms."""
        raw = os.getenv("TR_MCP_READ_MIN_INTERVAL_SEC", "0.15")
        try:
            interval = float(raw or "0.15")
        except ValueError:
            interval = 0.15
        if interval <= 0:
            return
        async with self._read_lock:
            now = time.monotonic()
            wait = self._last_read_at + interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_read_at = time.monotonic()

    async def _query(self, coro: Any) -> Any:
        """Fire one async subscription and wait for a single response."""
        try:
            await coro
            return await asyncio.wait_for(
                self._api.start(receive_one=True), timeout=self._timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await self._reset_transport()
            raise
        except Exception:
            # Ensure one-shot receive state cannot brick the client.
            if self._api.started:
                await self._reset_transport()
            raise

    @staticmethod
    def _as_coro_factory(coro_or_factory: Any):
        """Normalize a coroutine or zero-arg factory for retryable queries."""
        if asyncio.iscoroutine(coro_or_factory):
            box: list[Any] = [coro_or_factory]

            def once() -> Any:
                if not box:
                    raise TradeRepublicClientError(
                        "Cannot retry Trade Republic query: subscription was already consumed. "
                        "Call the tool again once — the adapter will renew the session first.",
                        retryable=True,
                        kind=ErrorKind.SESSION_EXPIRED,
                        retry_after_seconds=2,
                    )
                return box.pop()

            return once
        if callable(coro_or_factory):
            return coro_or_factory
        raise TypeError("Expected coroutine or zero-arg callable factory")

    async def _query_auth(self, coro_or_factory: Any, *, mutating: bool = False) -> Any:
        """Authenticated WebSocket query with autonomous session renew on auth failure."""
        factory = self._as_coro_factory(coro_or_factory)
        if mutating:
            await self._ensure_session_for_write()
        else:
            await self._ensure_session(allow_login=True)
            await self._maybe_resume_inflight_push()
            await self._throttle_read()
        try:
            result = await self._query(factory())
            if mutating:
                self._persist_cookies()
            return result
        except (TRapiException, TRapiExcServerErrorState) as exc:
            mapped = self._map_error(exc)
            if mapped.kind not in {
                ErrorKind.SESSION_EXPIRED,
                ErrorKind.SERVER,
                ErrorKind.LOGIN_REQUIRED,
                ErrorKind.AUTH_FAILED,
            }:
                raise mapped from exc
            if self._circuit.is_open():
                raise mapped from exc
            recovered = await self._recover_session()
            if not recovered:
                raise TradeRepublicClientError(
                    (
                        "Trade Republic session is cold and automatic renew failed "
                        f"(HTTP {self._last_renew_http_status or '401'}). "
                        "Confirm the Trade Republic app push if one was sent, then "
                        "call this same tool again. Do NOT fall back to other market-data "
                        "providers for account/portfolio tools."
                    ),
                    retryable=True,
                    kind=ErrorKind.LOGIN_REQUIRED,
                    retry_after_seconds=self._circuit.remaining_cooldown_seconds() or 30,
                ) from exc
            try:
                result = await self._query(factory())
                if mutating:
                    self._persist_cookies()
                return result
            except (TRapiException, TRapiExcServerErrorState) as retry_exc:
                raise self._map_error(retry_exc) from retry_exc

    async def _try_query_auth(self, coro_or_factory: Any) -> Any | None:
        """Authenticated query; returns None when TR has no data for this field."""
        try:
            return await self._query_auth(coro_or_factory)
        except TradeRepublicClientError:
            return None

    async def _query_public(self, coro_or_factory: Any) -> Any:
        """WebSocket query that does not require a logged-in session.

        Expired ``tr_session`` cookies are still attached to ``wss://`` connects and
        can yield HTTP 401 for charts/search. On auth failure, drop the cookie and
        retry once anonymously — do not trip the auth circuit for that case.
        """
        factory = self._as_coro_factory(coro_or_factory)
        try:
            return await self._query(factory())
        except (TRapiException, TRapiExcServerErrorState) as exc:
            mapped = self._map_error(exc, record=False)
            if mapped.kind not in {
                ErrorKind.SESSION_EXPIRED,
                ErrorKind.LOGIN_REQUIRED,
                ErrorKind.AUTH_FAILED,
            }:
                raise mapped from exc
            if not self._api._has_tr_session_cookie() and not self._api.sessionToken:
                raise mapped from exc
            LOGGER.info(
                "Public WS rejected with %s — clearing dead tr_session and retrying anonymously",
                mapped.kind.value,
            )
            self._api.clear_tr_session_cookie()
            self._session_ready = False
            await self._reset_transport()
            try:
                return await self._query(factory())
            except (TRapiException, TRapiExcServerErrorState) as retry_exc:
                raise self._map_error(retry_exc, record=False) from retry_exc

    @staticmethod
    def _normalize_isin(ticker: str) -> str:
        return ticker.strip().upper()

    async def _find_position(self, isin: str) -> dict[str, Any] | None:
        if not self._has_credentials:
            return None
        holdings = await self.get_holdings()
        return next((h for h in holdings if h.get("ticker") == isin), None)

    async def get_stock_analysis(
        self,
        ticker: str,
        *,
        include_position: bool = False,
    ) -> dict[str, Any]:
        """Fundamental stock analysis: details, KPIs, dividends, performance."""
        isin = self._normalize_isin(ticker)
        instrument = await self._query_auth(lambda: self._api.instrument(isin))
        details = await self._query_auth(lambda: self._api.stock_details(isin))
        kpis = await self._try_query_auth(lambda: self._api.stock_detail_kpis(isin))
        dividends = await self._try_query_auth(lambda: self._api.stock_detail_dividends(isin))
        performance = await self._try_query_auth(lambda: self._api.performance(isin))
        position = await self._find_position(isin) if include_position else None
        return {
            "ticker": isin,
            "instrument": instrument,
            "details": details,
            "kpis": kpis,
            "dividends": dividends,
            "performance": performance,
            "position": position,
        }

    async def get_etf_analysis(
        self,
        ticker: str,
        *,
        include_position: bool = False,
    ) -> dict[str, Any]:
        """ETF analysis: details and portfolio composition."""
        isin = self._normalize_isin(ticker)
        instrument = await self._query_auth(lambda: self._api.instrument(isin))
        details = await self._query_auth(lambda: self._api.etf_details(isin))
        composition = await self._try_query_auth(lambda: self._api.etf_composition(isin))
        position = await self._find_position(isin) if include_position else None
        return {
            "ticker": isin,
            "instrument": instrument,
            "details": details,
            "composition": composition,
            "position": position,
        }

    async def get_crypto_analysis(
        self,
        ticker: str,
        *,
        include_position: bool = False,
    ) -> dict[str, Any]:
        """Crypto asset analysis."""
        isin = self._normalize_isin(ticker)
        instrument = await self._query_auth(lambda: self._api.instrument(isin))
        details = await self._query_auth(lambda: self._api.crypto_details(isin))
        position = await self._find_position(isin) if include_position else None
        return {
            "ticker": isin,
            "instrument": instrument,
            "details": details,
            "position": position,
        }

    INSTRUMENT_TYPES = TRApi.instrument_list
    RANGE_VALUES = TRApi.range_list
    EXCHANGES = TRApi.exchange_list

    @staticmethod
    def _default_jurisdiction(explicit: str | None = None) -> str:
        if explicit:
            return explicit.upper()
        return os.getenv("TR_JURISDICTION", os.getenv("TR_LOCALE", "de")).upper()

    async def search_instruments(
        self,
        query: str,
        instrument_type: str = "stock",
        jurisdiction: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Search Trade Republic instruments by name or keyword (no login required)."""
        jurisdiction = self._default_jurisdiction(jurisdiction)
        if instrument_type not in self.INSTRUMENT_TYPES:
            raise TradeRepublicClientError(
                f"instrument_type must be one of {self.INSTRUMENT_TYPES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        results = await self._query_public(lambda: self._api.neon_search(
                query=query,
                page=page,
                page_size=page_size,
                instrument_type=instrument_type,
                jurisdiction=jurisdiction,
            ))
        return {
            "query": query,
            "instrument_type": instrument_type,
            "jurisdiction": jurisdiction,
            "page": page,
            "page_size": page_size,
            "results": results,
        }

    async def search_instruments_aggregations(
        self,
        query: str,
        instrument_type: str = "stock",
        jurisdiction: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Faceted search aggregations by category (no login required)."""
        jurisdiction = self._default_jurisdiction(jurisdiction)
        if instrument_type not in self.INSTRUMENT_TYPES:
            raise TradeRepublicClientError(
                f"instrument_type must be one of {self.INSTRUMENT_TYPES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        aggregations = await self._query_public(lambda: self._api.neon_search_aggregations(
                query=query,
                page=page,
                page_size=page_size,
                instrument_type=instrument_type,
                jurisdiction=jurisdiction,
            ))
        return {
            "query": query,
            "instrument_type": instrument_type,
            "jurisdiction": jurisdiction,
            "page": page,
            "page_size": page_size,
            "aggregations": aggregations,
        }

    async def get_search_tags(self) -> dict[str, Any]:
        """Available neon search tags (no login required)."""
        tags = await self._query_public(lambda: self._api.neon_search_tags())
        return {"tags": tags}

    async def get_search_suggested_tags(self, query: str = "") -> dict[str, Any]:
        """Suggested search tags for a query string (no login required)."""
        suggestions = await self._query_public(lambda: self._api.neon_search_suggested_tags(query=query))
        return {"query": query, "suggested_tags": suggestions}

    async def get_price_history(
        self,
        ticker: str,
        range: str = "1y",
        exchange: str = "LSX",
    ) -> dict[str, Any]:
        """Price history for any ISIN (no login required)."""
        isin = ticker.strip().upper()
        if range not in self.RANGE_VALUES:
            raise TradeRepublicClientError(
                f"range must be one of {self.RANGE_VALUES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        if exchange not in self.EXCHANGES:
            raise TradeRepublicClientError(
                f"exchange must be one of {self.EXCHANGES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        history = await self._query_public(lambda: self._api.aggregate_history_light(isin, range=range, exchange=exchange))
        return {
            "ticker": isin,
            "range": range,
            "exchange": exchange,
            "history": history,
        }

    async def get_stock_news(self, ticker: str) -> dict[str, Any]:
        """News articles for an ISIN (no login required)."""
        isin = ticker.strip().upper()
        news = await self._query_public(lambda: self._api.neon_news(isin))
        return {"ticker": isin, "news": news}

    @staticmethod
    def _unwrap_cash(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            if "accounts" in payload and isinstance(payload["accounts"], list):
                return payload["accounts"]
            return [payload]
        return []

    async def get_balance_info(self) -> dict[str, Any]:
        """Cash balances and buying power (read-only)."""
        cash = await self._query_auth(lambda: self._api.cash())
        available = await self._query_auth(lambda: self._api.available_cash())
        payout = await self._query_auth(lambda: self._api.available_cash_for_payout())
        status = await self._query_auth(lambda: self._api.portfolio_status())

        cash_accounts = self._unwrap_cash(cash)
        available_accounts = self._unwrap_cash(available)

        total_cash = sum(float(a.get("amount", 0) or 0) for a in cash_accounts)
        buying_power = sum(float(a.get("amount", 0) or 0) for a in available_accounts)

        return {
            "cash_accounts": cash_accounts,
            "available_cash_accounts": available_accounts,
            "available_cash_for_payout": payout,
            "portfolio_status": status,
            "summary": {
                "total_cash": total_cash,
                "buying_power": buying_power,
                "currency": (cash_accounts[0].get("currencyId") if cash_accounts else "EUR"),
            },
        }

    @staticmethod
    def _normalize_position(raw: dict[str, Any]) -> dict[str, Any]:
        ticker = raw.get("isin") or raw.get("instrumentId") or raw.get("id")
        quantity = raw.get("netSize") or raw.get("virtualSize") or raw.get("size")
        return {
            "ticker": ticker,
            "name": raw.get("name"),
            "quantity": quantity,
            "average_buy_in": raw.get("averageBuyIn"),
            "instrument_type": raw.get("instrumentType"),
            "status": raw.get("status"),
            "profit_loss": raw.get("profitLoss") or raw.get("profit") or raw.get("relativePerformance"),
            "category": raw.get("_category"),
        }

    async def _load_portfolio(self) -> dict[str, Any]:
        return await self._query_auth(lambda: self._api.compact_portfolio_by_type())

    async def get_holdings(self) -> list[dict[str, Any]]:
        """All active portfolio positions."""
        portfolio = await self._load_portfolio()
        positions: list[dict[str, Any]] = []

        categories = portfolio.get("categories") or []
        if categories:
            for category in categories:
                cat_type = category.get("categoryType")
                for pos in category.get("positions") or []:
                    item = self._normalize_position(pos)
                    item["category"] = cat_type
                    if item.get("status", "ACTIVE") != "INACTIVE":
                        positions.append(item)
            return positions

        for pos in portfolio.get("positions") or []:
            item = self._normalize_position(pos)
            if item.get("status", "ACTIVE") != "INACTIVE":
                positions.append(item)
        return positions

    async def get_ticker_details(self, ticker: str, *, include_position: bool = True) -> dict[str, Any]:
        """Instrument summary for one ISIN; includes portfolio line when held."""
        analysis = await self.get_stock_analysis(ticker, include_position=include_position)
        return {
            "ticker": analysis["ticker"],
            "instrument": analysis["instrument"],
            "stock_details": analysis["details"],
            "performance": analysis["performance"],
            "position": analysis["position"],
        }

    async def get_portfolio_history(self, range: str = "max") -> dict[str, Any]:
        """Portfolio value history over time (login required)."""
        if range not in self.RANGE_VALUES:
            raise TradeRepublicClientError(
                f"range must be one of {self.RANGE_VALUES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        history = await self._query_auth(lambda: self._api.portfolio_aggregate_history(range=range))
        return {"range": range, "history": history}

    @staticmethod
    def _watchlist_contains(payload: Any, isin: str) -> bool:
        rows: list[Any]
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = []
            for key in ("instruments", "positions", "items", "data", "watchlist"):
                val = payload.get(key)
                if isinstance(val, list):
                    rows = val
                    break
            if not rows:
                rows = [payload]
        else:
            rows = []
        needle = isin.strip().upper()
        for row in rows:
            if not isinstance(row, dict):
                continue
            instrument = row.get("instrument")
            candidate = (
                row.get("isin")
                or row.get("instrumentId")
                or row.get("ticker")
                or (instrument.get("isin") if isinstance(instrument, dict) else None)
            )
            if isinstance(candidate, str) and candidate.strip().upper() == needle:
                return True
        return False

    async def _verify_watchlist_membership(self, isin: str) -> bool | None:
        """Return True/False if membership is known, None if verify failed/timed out."""
        try:
            watchlist = await self._query_auth(lambda: self._api.watchlist())
        except Exception as exc:  # noqa: BLE001 — verify must not mask write outcome
            LOGGER.info("Watchlist verify failed: %s", redact_secrets(str(exc)))
            return None
        return self._watchlist_contains(watchlist, isin)

    async def get_watchlist(self) -> dict[str, Any]:
        """Current watchlist instruments (login required)."""
        watchlist = await self._query_auth(lambda: self._api.watchlist())
        items = watchlist if isinstance(watchlist, list) else watchlist
        return {"watchlist": items}

    async def add_to_watchlist(self, ticker: str) -> dict[str, Any]:
        """Add an ISIN to the account watchlist (mutating, login required)."""
        isin = self._normalize_isin(ticker)
        self._guard_write_backoff("add_to_watchlist")
        result = await self._query_auth(lambda: self._api.add_to_watchlist(isin), mutating=True)
        present = await self._verify_watchlist_membership(isin)
        if present is True:
            self._last_uncertain_write_at = None
            self._last_uncertain_write_action = None
            return {
                "status": "completed",
                "action": "add_to_watchlist",
                "ticker": isin,
                "verified": True,
                "result": result,
            }
        if present is False:
            return {
                "status": "error",
                "code": "watchlist_verify_failed",
                "action": "add_to_watchlist",
                "ticker": isin,
                "verified": False,
                "message": "Add acknowledged but ISIN not found on watchlist",
                "result": result,
                **self._note_uncertain_write("add_to_watchlist"),
            }
        return {
            "status": "uncertain",
            "code": "watchlist_verify_timeout",
            "action": "add_to_watchlist",
            "ticker": isin,
            "verified": None,
            "message": "Add sent but watchlist membership could not be verified",
            "result": result,
            **self._note_uncertain_write("add_to_watchlist"),
        }

    async def remove_from_watchlist(self, ticker: str) -> dict[str, Any]:
        """Remove an ISIN from the account watchlist (mutating, login required)."""
        isin = self._normalize_isin(ticker)
        self._guard_write_backoff("remove_from_watchlist")
        result = await self._query_auth(lambda: self._api.remove_from_watchlist(isin), mutating=True)
        present = await self._verify_watchlist_membership(isin)
        if present is False:
            self._last_uncertain_write_at = None
            self._last_uncertain_write_action = None
            return {
                "status": "completed",
                "action": "remove_from_watchlist",
                "ticker": isin,
                "verified": True,
                "result": result,
            }
        if present is True:
            return {
                "status": "error",
                "code": "watchlist_verify_failed",
                "action": "remove_from_watchlist",
                "ticker": isin,
                "verified": False,
                "message": "Remove acknowledged but ISIN still present on watchlist",
                "result": result,
                **self._note_uncertain_write("remove_from_watchlist"),
            }
        return {
            "status": "uncertain",
            "code": "watchlist_verify_timeout",
            "action": "remove_from_watchlist",
            "ticker": isin,
            "verified": None,
            "message": "Remove sent but watchlist absence could not be verified",
            "result": result,
            **self._note_uncertain_write("remove_from_watchlist"),
        }

    async def instrument_label(self, ticker: str) -> str | None:
        """Best-effort instrument name for confirmation prompts."""
        isin = self._normalize_isin(ticker)
        try:
            data = await self._query_public(lambda: self._api.instrument(isin))
            if isinstance(data, dict):
                return data.get("name") or data.get("shortName")
        except TradeRepublicClientError:
            return None
        return None

    @staticmethod
    def _extract_list(payload: Any, *preferred_keys: str) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in preferred_keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return value
            for key in ("data", "items", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    @classmethod
    def _extract_orders(cls, payload: Any) -> list[Any]:
        """Pull order objects from the various TR ``orders`` response shapes."""
        if isinstance(payload, list):
            return [o for o in payload if isinstance(o, dict)]
        if not isinstance(payload, dict):
            return []

        for key in ("orders", "openOrders", "activeOrders"):
            value = payload.get(key)
            if isinstance(value, list):
                return [o for o in value if isinstance(o, dict)]

        collected: list[Any] = []
        for key, value in payload.items():
            if key in {"cursors", "meta", "type"}:
                continue
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
                sample = value[0]
                if any(
                    k in sample
                    for k in (
                        "id",
                        "orderId",
                        "isin",
                        "instrumentId",
                        "instrument",
                        "type",
                        "side",
                        "status",
                        "state",
                    )
                ):
                    collected.extend(value)
            elif isinstance(value, dict):
                nested = cls._extract_orders(value)
                if nested:
                    collected.extend(nested)

        if collected:
            seen: set[str] = set()
            unique: list[Any] = []
            for order in collected:
                oid = str(
                    order.get("id")
                    or order.get("orderId")
                    or order.get("clientProcessId")
                    or ""
                )
                if oid and oid in seen:
                    continue
                if oid:
                    seen.add(oid)
                unique.append(order)
            return unique

        # Last resort: generic list containers.
        return [
            o
            for o in cls._extract_list(payload, "data", "items", "results")
            if isinstance(o, dict)
        ]

    @classmethod
    def _normalize_order(cls, order: dict[str, Any]) -> dict[str, Any]:
        """Stable summary fields for Hermes plus the raw TR payload."""
        instrument = order.get("instrument")
        instrument_id = None
        name = None
        if isinstance(instrument, dict):
            instrument_id = instrument.get("isin") or instrument.get("id")
            name = instrument.get("name") or instrument.get("shortName")
        elif isinstance(instrument, str):
            instrument_id = instrument

        isin = (
            order.get("isin")
            or order.get("instrumentId")
            or instrument_id
            or order.get("underlyingIsin")
        )
        if isinstance(isin, str):
            isin = isin.strip().upper() or None

        order_id = (
            order.get("id")
            or order.get("orderId")
            or order.get("clientProcessId")
            or order.get("order_id")
        )
        side = order.get("side") or order.get("type") or order.get("orderType")
        if isinstance(side, str):
            side = side.strip().lower()

        return {
            "order_id": order_id,
            "ticker": isin,
            "name": name or order.get("name") or order.get("instrumentName"),
            "side": side,
            "status": order.get("status") or order.get("state") or order.get("orderStatus"),
            "size": order.get("size")
            or order.get("netSize")
            or order.get("quantity")
            or order.get("amount"),
            "limit": order.get("limit") or order.get("limitPrice"),
            "stop": order.get("stop") or order.get("stopPrice"),
            "exchange": order.get("exchange")
            or order.get("exchangeId")
            or order.get("venue"),
            "created_at": order.get("createdAt")
            or order.get("created_at")
            or order.get("timestamp")
            or order.get("created"),
            "expiry": order.get("expiry") or order.get("expirationDate") or order.get("validUntil"),
            "raw": order,
        }

    @classmethod
    def _filter_orders_by_ticker(
        cls, orders: list[dict[str, Any]], ticker: str | None
    ) -> list[dict[str, Any]]:
        if not ticker:
            return orders
        needle = ticker.strip().upper()
        if not needle:
            return orders
        filtered: list[dict[str, Any]] = []
        for order in orders:
            summary = cls._normalize_order(order) if "raw" not in order else order
            candidate = summary.get("ticker") or ""
            if candidate == needle or needle in str(candidate):
                filtered.append(order)
        return filtered

    @classmethod
    def _extract_timeline_items(cls, payload: Any) -> list[Any]:
        return cls._extract_list(payload, "events", "timeline", "data", "items", "results")

    def _validate_limit(self, limit: int) -> None:
        if limit < 1 or limit > 100:
            raise TradeRepublicClientError(
                "limit must be between 1 and 100",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )

    async def get_recent_transactions(
        self,
        limit: int = 20,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Recent account transactions from timeline (login required)."""
        self._validate_limit(limit)
        raw = await self._query_auth(lambda: self._api.timeline_transactions(after=after))
        items = self._extract_timeline_items(raw)
        return {
            "after": after,
            "limit": limit,
            "count": min(len(items), limit),
            "transactions": items[:limit],
        }

    async def get_full_timeline(
        self,
        limit: int = 20,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Full account timeline (broader than cash-relevant transactions)."""
        self._validate_limit(limit)
        raw = await self._query_auth(lambda: self._api.timeline(after=after))
        items = self._extract_timeline_items(raw)
        next_cursor = None
        if isinstance(raw, dict):
            next_cursor = raw.get("cursors", {}).get("after") if isinstance(
                raw.get("cursors"), dict
            ) else raw.get("after")
        return {
            "after": after,
            "next_after": next_cursor,
            "limit": limit,
            "count": min(len(items), limit),
            "events": items[:limit],
        }

    async def get_transaction_detail(self, event_id: str) -> dict[str, Any]:
        """Timeline event detail (documents, tax info) via timelineDetailV2."""
        cleaned = (event_id or "").strip()
        if not cleaned:
            raise TradeRepublicClientError(
                "event_id must not be empty",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        detail = await self._query_auth(lambda: self._api.timeline_detail_v2(cleaned))
        return {"event_id": cleaned, "detail": detail}

    async def list_open_orders(
        self,
        *,
        include_terminated: bool = False,
        ticker: str | None = None,
    ) -> dict[str, Any]:
        """Open (or optionally terminated) orders for the account."""
        raw = await self._query_auth(lambda: self._api.orders(terminated=include_terminated))
        orders = self._extract_orders(raw)
        if ticker:
            isin = self._normalize_isin(ticker)
            orders = self._filter_orders_by_ticker(orders, isin)
        else:
            isin = None
        summaries = [self._normalize_order(o) for o in orders if isinstance(o, dict)]
        return {
            "include_terminated": include_terminated,
            "ticker": isin,
            "count": len(summaries),
            "orders": summaries,
        }

    async def list_order_history(
        self,
        *,
        ticker: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Filled / cancelled / otherwise terminated orders (read-only)."""
        self._validate_limit(limit)
        raw = await self._query_auth(lambda: self._api.orders(terminated=True))
        orders = self._extract_orders(raw)
        if ticker:
            isin = self._normalize_isin(ticker)
            orders = self._filter_orders_by_ticker(orders, isin)
        else:
            isin = None
        summaries = [self._normalize_order(o) for o in orders if isinstance(o, dict)]
        return {
            "ticker": isin,
            "limit": limit,
            "count": min(len(summaries), limit),
            "orders": summaries[:limit],
        }

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch one order by id from open + terminated lists; enrich with timeline detail."""
        cleaned = (order_id or "").strip()
        if not cleaned:
            raise TradeRepublicClientError(
                "order_id must not be empty",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )

        matched: dict[str, Any] | None = None
        source = None
        for terminated, label in ((False, "open"), (True, "history")):
            raw = await self._query_auth(lambda t=terminated: self._api.orders(terminated=t))
            for order in self._extract_orders(raw):
                if not isinstance(order, dict):
                    continue
                summary = self._normalize_order(order)
                oid = str(summary.get("order_id") or "")
                if oid == cleaned:
                    matched = summary
                    source = label
                    break
            if matched is not None:
                break

        detail = None
        try:
            detail = await self._query_auth(
                lambda: self._api.timeline_detail_order(cleaned)
            )
        except TradeRepublicClientError:
            detail = None

        if matched is None and detail is None:
            raise TradeRepublicClientError(
                f"Order {cleaned!r} not found in open or terminated orders.",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )

        return {
            "order_id": cleaned,
            "found_in": source,
            "order": matched,
            "detail": detail,
        }

    async def list_savings_plans(self) -> dict[str, Any]:
        """Active savings plans for the account."""
        raw = await self._query_auth(lambda: self._api.savings_plans())
        plans = self._extract_list(raw, "savingsPlans", "plans", "data", "items")
        return {"count": len(plans), "savings_plans": plans}

    async def list_price_alarms(self) -> dict[str, Any]:
        """Active price alarms for the account."""
        raw = await self._query_auth(lambda: self._api.price_alarms())
        alarms = self._extract_list(raw, "priceAlarms", "alarms", "data", "items")
        return {"count": len(alarms), "price_alarms": alarms}

    PRODUCT_CATEGORIES = TRApi.product_category_list
    ORDER_TYPES = TRApi.order_type_list

    def _validate_exchange(self, exchange: str) -> str:
        cleaned = (exchange or "").strip().upper()
        if cleaned not in self.EXCHANGES:
            raise TradeRepublicClientError(
                f"exchange must be one of {self.EXCHANGES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        return cleaned

    async def get_live_quote(
        self,
        ticker: str,
        exchange: str = "LSX",
    ) -> dict[str, Any]:
        """One-shot live quote snapshot from the ticker stream (no login required)."""
        isin = self._normalize_isin(ticker)
        exchange = self._validate_exchange(exchange)
        quote = await self._query_public(lambda: self._api.ticker(isin, exchange=exchange))
        return {"ticker": isin, "exchange": exchange, "quote": quote}

    async def get_derivatives(
        self,
        ticker: str,
        product_category: str = "vanillaWarrant",
    ) -> dict[str, Any]:
        """Derivatives (warrants / knock-outs / factors) for an underlying ISIN."""
        isin = self._normalize_isin(ticker)
        category = (product_category or "").strip()
        if category not in self.PRODUCT_CATEGORIES:
            raise TradeRepublicClientError(
                f"product_category must be one of {self.PRODUCT_CATEGORIES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        raw = await self._query_auth(lambda: self._api.derivatives(isin, category))
        items = self._extract_list(raw, "derivatives", "instruments", "data", "items", "results")
        return {
            "ticker": isin,
            "product_category": category,
            "count": len(items),
            "derivatives": items,
        }

    async def get_instrument_suitability(self, ticker: str) -> dict[str, Any]:
        """Suitability / compliance info for an instrument (pre-trade)."""
        isin = self._normalize_isin(ticker)
        suitability = await self._query_auth(lambda: self._api.instrument_suitability(isin))
        return {"ticker": isin, "suitability": suitability}

    async def get_order_preview(
        self,
        ticker: str,
        order_type: str = "buy",
        exchange: str = "LSX",
    ) -> dict[str, Any]:
        """Read-only pre-trade preview: indicative price and available size."""
        isin = self._normalize_isin(ticker)
        exchange = self._validate_exchange(exchange)
        side = (order_type or "").strip().lower()
        if side not in self.ORDER_TYPES:
            raise TradeRepublicClientError(
                f"order_type must be one of {self.ORDER_TYPES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        price = await self._query_auth(lambda: self._api.price_for_order(isin, exchange=exchange, order_type=side))
        size = await self._query_auth(lambda: self._api.available_size(isin, exchange=exchange))
        return {
            "ticker": isin,
            "exchange": exchange,
            "order_type": side,
            "price": price,
            "available_size": size,
        }

    async def get_account_settings(self) -> dict[str, Any]:
        """Account settings for the logged-in user."""
        settings = await self._query_auth(lambda: self._api.settings())
        return {"settings": settings}

    async def get_account_pairs(self) -> dict[str, Any]:
        """Securities/cash account numbers including tax wrappers."""
        pairs = await self._query_auth(lambda: self._api.account_pairs())
        return {"account_pairs": pairs}

    EXPIRIES = TRApi.expiry_list

    def _validate_order_side(self, order_type: str) -> str:
        side = (order_type or "").strip().lower()
        if side not in self.ORDER_TYPES:
            raise TradeRepublicClientError(
                f"order_type must be one of {self.ORDER_TYPES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        return side

    def _validate_expiry(self, expiry: str, expiry_date: str | None) -> tuple[str, str | None]:
        cleaned = (expiry or "").strip().lower()
        if cleaned not in self.EXPIRIES:
            raise TradeRepublicClientError(
                f"expiry must be one of {self.EXPIRIES}",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        date = (expiry_date or "").strip() or None
        if cleaned == "gtd" and not date:
            raise TradeRepublicClientError(
                "expiry_date is required when expiry is 'gtd'",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        return cleaned, date

    @staticmethod
    def _validate_positive(name: str, value: float | int) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise TradeRepublicClientError(
                f"{name} must be a number",
                retryable=False,
                kind=ErrorKind.CONFIG,
            ) from exc
        if number <= 0:
            raise TradeRepublicClientError(
                f"{name} must be > 0",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        return number

    @staticmethod
    def _order_id_of(order: Any) -> str | None:
        if not isinstance(order, dict):
            return None
        for key in ("id", "orderId", "order_id"):
            value = order.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _order_isin_of(order: Any) -> str | None:
        if not isinstance(order, dict):
            return None
        for key in ("isin", "instrumentId", "instrument_id"):
            value = order.get(key)
            if value:
                return str(value).upper()
        params = order.get("parameters")
        if isinstance(params, dict):
            for key in ("instrumentId", "isin"):
                value = params.get(key)
                if value:
                    return str(value).upper()
        return None

    async def _find_open_order(
        self,
        *,
        isin: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any] | None:
        listed = await self.list_open_orders(include_terminated=False)
        for order in listed.get("orders") or []:
            if order_id and self._order_id_of(order) == order_id:
                return order if isinstance(order, dict) else {"id": order_id}
            if isin and self._order_isin_of(order) == isin:
                return order if isinstance(order, dict) else {"isin": isin}
        return None

    async def place_limit_order(
        self,
        ticker: str,
        order_type: str,
        size: float,
        limit: float,
        expiry: str = "gfd",
        exchange: str = "LSX",
        expiry_date: str | None = None,
    ) -> dict[str, Any]:
        """Place a limit order (real money). Caller must gate with trading confirm."""
        isin = self._normalize_isin(ticker)
        side = self._validate_order_side(order_type)
        exchange = self._validate_exchange(exchange)
        expiry, expiry_date = self._validate_expiry(expiry, expiry_date)
        size_n = self._validate_positive("size", size)
        limit_n = self._validate_positive("limit", limit)
        self._guard_write_backoff("place_limit_order")
        result = await self._query_auth(
            self._api.limit_order(
                isin,
                side,
                size_n,
                limit_n,
                expiry,
                exchange=exchange,
                expiry_date=expiry_date,
            ),
            mutating=True,
        )
        found = await self._find_open_order(isin=isin)
        base = {
            "action": "place_limit_order",
            "ticker": isin,
            "order_type": side,
            "size": size_n,
            "limit": limit_n,
            "expiry": expiry,
            "expiry_date": expiry_date,
            "exchange": exchange,
            "result": result,
        }
        if found is not None:
            self._last_uncertain_write_at = None
            self._last_uncertain_write_action = None
            return {
                "status": "completed",
                "verified": True,
                "order": found,
                **base,
            }
        return {
            "status": "uncertain",
            "code": "order_verify_timeout",
            "verified": None,
            "message": "Limit order sent but not found in open orders yet",
            **base,
            **self._note_uncertain_write("place_limit_order"),
        }

    async def place_stop_market_order(
        self,
        ticker: str,
        order_type: str,
        size: float,
        stop: float,
        expiry: str = "gtc",
        exchange: str = "LSX",
        expiry_date: str | None = None,
    ) -> dict[str, Any]:
        """Place a stop-market order / stop-loss (real money)."""
        isin = self._normalize_isin(ticker)
        side = self._validate_order_side(order_type)
        exchange = self._validate_exchange(exchange)
        expiry, expiry_date = self._validate_expiry(expiry, expiry_date)
        size_n = self._validate_positive("size", size)
        stop_n = self._validate_positive("stop", stop)
        self._guard_write_backoff("place_stop_market_order")
        result = await self._query_auth(
            self._api.stop_market_order(
                isin,
                side,
                size_n,
                stop_n,
                expiry,
                exchange=exchange,
                expiry_date=expiry_date,
            ),
            mutating=True,
        )
        found = await self._find_open_order(isin=isin)
        base = {
            "action": "place_stop_market_order",
            "ticker": isin,
            "order_type": side,
            "size": size_n,
            "stop": stop_n,
            "expiry": expiry,
            "expiry_date": expiry_date,
            "exchange": exchange,
            "result": result,
        }
        if found is not None:
            self._last_uncertain_write_at = None
            self._last_uncertain_write_action = None
            return {
                "status": "completed",
                "verified": True,
                "order": found,
                **base,
            }
        return {
            "status": "uncertain",
            "code": "order_verify_timeout",
            "verified": None,
            "message": "Stop-market order sent but not found in open orders yet",
            **base,
            **self._note_uncertain_write("place_stop_market_order"),
        }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an open order by id (real money)."""
        cleaned = (order_id or "").strip()
        if not cleaned:
            raise TradeRepublicClientError(
                "order_id is required",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        self._guard_write_backoff("cancel_order")
        result = await self._query_auth(
            self._api.cancel_order(cleaned),
            mutating=True,
        )
        still_open = await self._find_open_order(order_id=cleaned)
        base = {
            "action": "cancel_order",
            "order_id": cleaned,
            "result": result,
        }
        if still_open is None:
            self._last_uncertain_write_at = None
            self._last_uncertain_write_action = None
            return {
                "status": "completed",
                "verified": True,
                **base,
            }
        return {
            "status": "uncertain",
            "code": "order_cancel_verify_failed",
            "verified": False,
            "message": "Cancel sent but order still appears in open orders",
            "order": still_open,
            **base,
            **self._note_uncertain_write("cancel_order"),
        }
