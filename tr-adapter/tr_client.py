"""Read-only Trade Republic client wrapper for the MCP adapter.

Wraps the repository's TrBlockingApi without modifying trapi/.
Credentials come from environment variables only (see .env.example).
"""

from __future__ import annotations

import os
import sys
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trapi.api import TRapiException, TRapiExcServerErrorState, TrBlockingApi  # noqa: E402


class TradeRepublicClientError(Exception):
    """Human-readable adapter error for MCP tools."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class TradeRepublicClient:
    """Read-only facade over TrBlockingApi for Hermes / MCP."""

    def __init__(self, token: str | None = None):
        self._token = token or os.getenv("TR_TOKEN")
        phone = os.getenv("TR_PHONE", "")
        pin = os.getenv("TR_PIN", "")
        locale = os.getenv("TR_LOCALE", "de")
        cookies_file = os.getenv("TR_COOKIES_FILE", "tr_cookies.txt")

        if not self._token and (not phone or not pin):
            raise TradeRepublicClientError(
                "Missing credentials. Set TR_TOKEN (session) or TR_PHONE and TR_PIN in the environment.",
                retryable=False,
            )

        self._api = TrBlockingApi(
            phone or "+0000000000",
            pin or "0000",
            locale=locale,
            cookies_file=cookies_file,
            auth="web",
        )
        self._session_ready = False

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

    def _ensure_session(self) -> None:
        if self._session_ready:
            return
        try:
            if self._token:
                self._inject_token_on_api()
            if self._api._resume_web_session():
                self._session_ready = True
                return
            if not os.getenv("TR_PHONE") or not os.getenv("TR_PIN"):
                raise TradeRepublicClientError(
                    "Session expired or invalid TR_TOKEN. Set TR_PHONE and TR_PIN, then confirm the app push on login.",
                    retryable=True,
                )
            self._api.login(resume=False)
            self._session_ready = True
        except TRapiException as exc:
            raise self._map_error(exc) from exc

    @staticmethod
    def _map_error(exc: Exception) -> TradeRepublicClientError:
        message = str(exc)
        lower = message.lower()
        if "not confirmed in time" in lower or "process_gone" in lower:
            return TradeRepublicClientError(
                "Login was not confirmed in the Trade Republic app in time. Retry and approve the push notification.",
                retryable=True,
            )
        if "session" in lower or "401" in lower or "login failed" in lower:
            return TradeRepublicClientError(
                "Trade Republic session expired or invalid. Refresh TR_TOKEN or log in again with TR_PHONE/TR_PIN.",
                retryable=True,
            )
        if isinstance(exc, TRapiExcServerErrorState):
            return TradeRepublicClientError(
                "Trade Republic websocket returned an error (subscription may have expired). Retry the request.",
                retryable=True,
            )
        if "connection error" in lower or "timeout" in lower:
            return TradeRepublicClientError(
                "Could not reach Trade Republic (network or server issue). Try again later.",
                retryable=True,
            )
        return TradeRepublicClientError(f"Trade Republic API error: {message}", retryable=True)

    @staticmethod
    def _unwrap_cash(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            if "accounts" in payload and isinstance(payload["accounts"], list):
                return payload["accounts"]
            return [payload]
        return []

    def get_balance_info(self) -> dict[str, Any]:
        """Cash balances and buying power (read-only)."""
        self._ensure_session()
        try:
            cash = self._api.cash()
            available = self._api.available_cash()
            payout = self._api.available_cash_for_payout()
            status = self._api.portfolio_status()
        except (TRapiException, TRapiExcServerErrorState) as exc:
            raise self._map_error(exc) from exc

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

    def _load_portfolio(self) -> dict[str, Any]:
        self._ensure_session()
        try:
            return self._api.compact_portfolio_by_type()
        except (TRapiException, TRapiExcServerErrorState) as exc:
            raise self._map_error(exc) from exc

    def get_holdings(self) -> list[dict[str, Any]]:
        """All active portfolio positions."""
        portfolio = self._load_portfolio()
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

    def get_ticker_details(self, ticker: str) -> dict[str, Any]:
        """Instrument and stock details for one ISIN/ticker."""
        self._ensure_session()
        isin = ticker.strip().upper()
        try:
            instrument = self._api.instrument(isin)
            details = self._api.stock_details(isin)
            performance = None
            try:
                performance = self._api.performance(isin)
            except (TRapiException, TRapiExcServerErrorState):
                performance = None
        except (TRapiException, TRapiExcServerErrorState) as exc:
            raise self._map_error(exc) from exc

        holding = next((h for h in self.get_holdings() if h.get("ticker") == isin), None)

        return {
            "ticker": isin,
            "instrument": instrument,
            "stock_details": details,
            "performance": performance,
            "position": holding,
        }
