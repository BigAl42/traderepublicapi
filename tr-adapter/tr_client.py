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
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trapi.api import TRApi, TRapiException, TRapiExcServerErrorState  # noqa: E402

from session import (  # noqa: E402
    AuthCircuitBreaker,
    ClassifiedError,
    ErrorKind,
    SessionBlockedError,
    circuit_state_path_for_cookies,
    classify_auth_error,
)

LOGGER = logging.getLogger("tr_adapter.client")


class TradeRepublicClientError(Exception):
    """Human-readable adapter error for MCP tools."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        kind: ErrorKind = ErrorKind.UNKNOWN,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds

    @classmethod
    def from_classified(cls, classified: ClassifiedError) -> TradeRepublicClientError:
        return cls(
            classified.message,
            retryable=classified.retryable,
            kind=classified.kind,
            retry_after_seconds=classified.retry_after_seconds,
        )


class TradeRepublicClient:
    """Async facade over TRApi for Hermes / MCP with hardened session handling."""

    def __init__(self, token: str | None = None):
        self._token = token or os.getenv("TR_TOKEN")
        phone = os.getenv("TR_PHONE", "")
        pin = os.getenv("TR_PIN", "")
        locale = os.getenv("TR_LOCALE", "de")
        cookies_file = os.getenv("TR_COOKIES_FILE", "tr_cookies.txt")
        self._timeout = float(os.getenv("TR_TIMEOUT", "20"))
        self._has_credentials = bool(self._token or (phone and pin))
        self._allow_interactive_login = os.getenv(
            "TR_MCP_ALLOW_INTERACTIVE_LOGIN", "0"
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
        try:
            self._api.reset_transport_sync()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Transport reset during invalidate failed: %s", exc)

    async def _reset_transport(self) -> None:
        try:
            await self._api.reset_transport()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Async transport reset failed: %s", exc)
            self._api.reset_transport_sync()

    def _map_error(self, exc: Exception) -> TradeRepublicClientError:
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
        if classified.kind in {
            ErrorKind.RATE_LIMITED,
            ErrorKind.AUTH_FAILED,
            ErrorKind.SESSION_EXPIRED,
        }:
            self._circuit.record_failure(classified)
            self._invalidate_session()
        return TradeRepublicClientError.from_classified(classified)

    def _try_resume(self) -> bool:
        """Cookie/token resume only — never starts a push login."""
        if self._token:
            self._inject_token_on_api()
        if self._api._resume_web_session():
            self._persist_cookies()
            self._session_ready = True
            self._circuit.record_success()
            # New HTTP session cookies must not reuse a stale websocket.
            self._api.reset_transport_sync()
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

    def _ensure_session(self, *, allow_login: bool = True) -> None:
        """Ensure a usable web session.

        Cookie-first. Interactive login only when allow_login=True and circuit is closed.
        """
        if self._session_ready:
            return
        if not self._has_credentials:
            raise TradeRepublicClientError(
                "Missing credentials. Set TR_TOKEN (session) or TR_PHONE and TR_PIN in the environment.",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )

        self._circuit.guard()

        try:
            if self._try_resume():
                return
            if not allow_login:
                raise TradeRepublicClientError(
                    "No warm Trade Republic session available for this action. "
                    "Refresh cookies offline with check_login.py, set TR_TOKEN, "
                    "and avoid write calls during auth cooldown.",
                    retryable=True,
                    kind=ErrorKind.LOGIN_REQUIRED,
                )
            self._interactive_login()
        except SessionBlockedError as exc:
            raise self._map_error(exc) from exc
        except TRapiException as exc:
            raise self._map_error(exc) from exc

    def _soft_refresh_session(self) -> bool:
        """Refresh web session endpoint without triggering login."""
        try:
            response = self._api._refresh_web_session()
            if getattr(response, "status_code", 500) >= 400:
                return False
            data = self._api.refresh_account_settings()
            if data is None:
                return False
            self._persist_cookies()
            self._session_ready = True
            # Force websocket reconnect with refreshed cookies.
            self._api.reset_transport_sync()
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Soft session refresh failed: %s", exc)
            return False

    def _ensure_session_for_write(self) -> None:
        """Warm session for mutating calls — resume + soft refresh, no login storm."""
        self._circuit.guard()
        if self._session_ready:
            if self._soft_refresh_session():
                return
            self._invalidate_session()

        self._ensure_session(allow_login=False)
        if not self._soft_refresh_session():
            self._invalidate_session()
            raise TradeRepublicClientError(
                "Session is not warm enough for mutating Trade Republic actions. "
                "Wait out any auth cooldown, refresh TR_TOKEN/cookies with check_login.py, "
                "then retry once.",
                retryable=True,
                kind=ErrorKind.SESSION_EXPIRED,
                retry_after_seconds=self._circuit.remaining_cooldown_seconds() or 60,
            )

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

    async def _query_auth(self, coro: Any, *, mutating: bool = False) -> Any:
        """Authenticated WebSocket query."""
        if mutating:
            self._ensure_session_for_write()
        else:
            self._ensure_session(allow_login=True)
        try:
            result = await self._query(coro)
            if mutating:
                self._persist_cookies()
            return result
        except (TRapiException, TRapiExcServerErrorState) as exc:
            mapped = self._map_error(exc)
            if (
                not mutating
                and mapped.kind in {ErrorKind.SESSION_EXPIRED, ErrorKind.SERVER}
                and not self._circuit.is_open()
            ):
                self._invalidate_session()
                try:
                    if self._try_resume():
                        return await self._query(coro)
                except (TRapiException, TRapiExcServerErrorState) as retry_exc:
                    raise self._map_error(retry_exc) from retry_exc
            raise mapped from exc

    async def _try_query_auth(self, coro: Any) -> Any | None:
        """Authenticated query; returns None when TR has no data for this field."""
        try:
            return await self._query_auth(coro)
        except TradeRepublicClientError:
            return None

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
        instrument = await self._query_auth(self._api.instrument(isin))
        details = await self._query_auth(self._api.stock_details(isin))
        kpis = await self._try_query_auth(self._api.stock_detail_kpis(isin))
        dividends = await self._try_query_auth(self._api.stock_detail_dividends(isin))
        performance = await self._try_query_auth(self._api.performance(isin))
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
        instrument = await self._query_auth(self._api.instrument(isin))
        details = await self._query_auth(self._api.etf_details(isin))
        composition = await self._try_query_auth(self._api.etf_composition(isin))
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
        instrument = await self._query_auth(self._api.instrument(isin))
        details = await self._query_auth(self._api.crypto_details(isin))
        position = await self._find_position(isin) if include_position else None
        return {
            "ticker": isin,
            "instrument": instrument,
            "details": details,
            "position": position,
        }

    async def _query_public(self, coro: Any) -> Any:
        """WebSocket query that does not require a logged-in session."""
        try:
            await coro
            return await asyncio.wait_for(
                self._api.start(receive_one=True), timeout=self._timeout
            )
        except (TRapiException, TRapiExcServerErrorState) as exc:
            raise self._map_error(exc) from exc

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
        results = await self._query_public(
            self._api.neon_search(
                query=query,
                page=page,
                page_size=page_size,
                instrument_type=instrument_type,
                jurisdiction=jurisdiction,
            )
        )
        return {
            "query": query,
            "instrument_type": instrument_type,
            "jurisdiction": jurisdiction,
            "page": page,
            "page_size": page_size,
            "results": results,
        }

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
        history = await self._query_public(
            self._api.aggregate_history_light(isin, range=range, exchange=exchange)
        )
        return {
            "ticker": isin,
            "range": range,
            "exchange": exchange,
            "history": history,
        }

    async def get_stock_news(self, ticker: str) -> dict[str, Any]:
        """News articles for an ISIN (no login required)."""
        isin = ticker.strip().upper()
        news = await self._query_public(self._api.neon_news(isin))
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
        cash = await self._query_auth(self._api.cash())
        available = await self._query_auth(self._api.available_cash())
        payout = await self._query_auth(self._api.available_cash_for_payout())
        status = await self._query_auth(self._api.portfolio_status())

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
        return await self._query_auth(self._api.compact_portfolio_by_type())

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
        history = await self._query_auth(
            self._api.portfolio_aggregate_history(range=range)
        )
        return {"range": range, "history": history}

    async def get_watchlist(self) -> dict[str, Any]:
        """Current watchlist instruments (login required)."""
        watchlist = await self._query_auth(self._api.watchlist())
        items = watchlist if isinstance(watchlist, list) else watchlist
        return {"watchlist": items}

    async def add_to_watchlist(self, ticker: str) -> dict[str, Any]:
        """Add an ISIN to the account watchlist (mutating, login required)."""
        isin = self._normalize_isin(ticker)
        result = await self._query_auth(self._api.add_to_watchlist(isin), mutating=True)
        return {
            "status": "completed",
            "action": "add_to_watchlist",
            "ticker": isin,
            "result": result,
        }

    async def remove_from_watchlist(self, ticker: str) -> dict[str, Any]:
        """Remove an ISIN from the account watchlist (mutating, login required)."""
        isin = self._normalize_isin(ticker)
        result = await self._query_auth(
            self._api.remove_from_watchlist(isin), mutating=True
        )
        return {
            "status": "completed",
            "action": "remove_from_watchlist",
            "ticker": isin,
            "result": result,
        }

    async def instrument_label(self, ticker: str) -> str | None:
        """Best-effort instrument name for confirmation prompts."""
        isin = self._normalize_isin(ticker)
        try:
            data = await self._query_public(self._api.instrument(isin))
            if isinstance(data, dict):
                return data.get("name") or data.get("shortName")
        except TradeRepublicClientError:
            return None
        return None

    @staticmethod
    def _extract_timeline_items(payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "items", "events", "timeline", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def get_recent_transactions(
        self,
        limit: int = 20,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Recent account transactions from timeline (login required)."""
        if limit < 1 or limit > 100:
            raise TradeRepublicClientError(
                "limit must be between 1 and 100",
                retryable=False,
                kind=ErrorKind.CONFIG,
            )
        raw = await self._query_auth(self._api.timeline_transactions(after=after))
        items = self._extract_timeline_items(raw)
        return {
            "after": after,
            "limit": limit,
            "count": min(len(items), limit),
            "transactions": items[:limit],
        }
