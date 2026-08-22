"""Trade Republic read-only MCP adapter (stdio transport for Hermes)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from pydantic.functional_validators import BeforeValidator
from typing import Annotated

from tr_client import TradeRepublicClient, TradeRepublicClientError
from mcp_write import (
    confirmation_required,
    require_confirmation,
    require_write_enabled,
)
from errors import raise_structured
from redact import redact_secrets

load_dotenv(Path(__file__).resolve().parent / ".env")

LOG_PATH = os.getenv(
    "TR_ADAPTER_LOG_PATH",
    "/srv/deployments/code-ide/hermes/data/logs/tr_adapter.log",
)

F = TypeVar("F", bound=Callable[..., Any])


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("tr_adapter")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # Always log to stderr (Docker / local). Optionally also to a Hermes file path.
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("Could not open log file %s; stderr only", LOG_PATH)

    return logger


LOGGER = _setup_logging()

mcp = FastMCP("TR-ReadOnly-Adapter")

_client: TradeRepublicClient | None = None


def get_client() -> TradeRepublicClient:
    global _client
    if _client is None:
        _client = TradeRepublicClient(token=os.getenv("TR_TOKEN"))
    return _client


def _normalize_ticker(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Ticker must be a string.")
    cleaned = value.strip().upper()
    if not cleaned.isalnum():
        raise ValueError("Ticker must contain only letters and digits (ISIN format).")
    if len(cleaned) < 6 or len(cleaned) > 12:
        raise ValueError("Ticker must be 6–12 alphanumeric characters (ISIN format).")
    return cleaned


class TickerInput(BaseModel):
    """ISIN or ticker symbol for a Trade Republic instrument."""

    ticker: Annotated[str, BeforeValidator(_normalize_ticker)] = Field(
        ...,
        description="ISIN, e.g. US0378331005",
    )


class SearchInstrumentsInput(BaseModel):
    """Parameters for neon search."""

    query: str = Field(..., min_length=1, max_length=200, description="Search text, e.g. Apple or semiconductor")
    instrument_type: str = Field(
        default="stock",
        description="Instrument type: stock, fund, derivative, or crypto",
    )
    jurisdiction: str = Field(default="DE", min_length=2, max_length=2, description="Country code, e.g. DE")
    page: int = Field(default=1, ge=1, description="Result page (1-based)")
    page_size: int = Field(default=20, ge=1, le=100, description="Results per page")


class PriceHistoryInput(BaseModel):
    """Parameters for aggregate price history."""

    ticker: Annotated[str, BeforeValidator(_normalize_ticker)] = Field(
        ...,
        description="ISIN, e.g. US0378331005",
    )
    range: str = Field(default="1y", description="Time range: 1d, 5d, 1m, 3m, 1y, max")
    exchange: str = Field(default="LSX", description="Exchange: LSX, TDG, LUS, TUB, BHS, B2C")


class PortfolioHistoryInput(BaseModel):
    """Parameters for portfolio aggregate history."""

    range: str = Field(default="max", description="Time range: 1d, 5d, 1m, 3m, 1y, max")


class TransactionsInput(BaseModel):
    """Parameters for recent timeline transactions."""

    limit: int = Field(default=20, ge=1, le=100, description="Max number of events to return")
    after: str | None = Field(default=None, description="Optional pagination cursor")


def log_tool_call(name: str) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = datetime.now(timezone.utc).isoformat()
                try:
                    result = await func(*args, **kwargs)
                    LOGGER.info("tool=%s status=success timestamp=%s", name, started)
                    return result
                except Exception as exc:
                    LOGGER.info(
                        "tool=%s status=error timestamp=%s detail=%s",
                        name,
                        started,
                        redact_secrets(str(exc)),
                    )
                    raise

            return async_wrapper  # type: ignore[return-value]

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = datetime.now(timezone.utc).isoformat()
            try:
                result = func(*args, **kwargs)
                LOGGER.info("tool=%s status=success timestamp=%s", name, started)
                return result
            except Exception as exc:
                LOGGER.info(
                    "tool=%s status=error timestamp=%s detail=%s",
                    name,
                    started,
                    redact_secrets(str(exc)),
                )
                raise

        return sync_wrapper  # type: ignore[return-value]

    return decorator


@mcp.tool()
@log_tool_call("get_adapter_status")
async def get_adapter_status() -> dict:
    """Local adapter health for Hermes: session, circuit breaker, write flags.

    Does not call Trade Republic. Use before retries when tools fail with
    rate_limited / login_required, or after an uncertain watchlist write.
    Respect retry_after_seconds — do not push-login during cooldown.
    """
    try:
        return get_client().get_adapter_status()
    except Exception as exc:
        raise_structured(exc)
        raise  # pragma: no cover


@mcp.tool()
@log_tool_call("get_account_summary")
async def get_account_summary() -> dict:
    """Return account cash balances, buying power, and portfolio status.

    Use this for a high-level financial overview: total cash, funds available
    to buy securities, cash available for payout, and overall portfolio status.
    Read-only; no orders or transfers.
    """
    try:
        return await get_client().get_balance_info()
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("list_active_positions")
async def list_active_positions() -> list:
    """List all currently held portfolio positions.

    Each item includes ticker (ISIN), name, quantity, average buy-in,
    instrument type, status, and profit/loss when provided by Trade Republic.
    Read-only.
    """
    try:
        return await get_client().get_holdings()
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("get_position_details")
async def get_position_details(ticker: str) -> dict:
    """Return detailed data for one position or instrument.

    Args:
        ticker: ISIN of the instrument (e.g. US0378331005 for Apple).

    Returns instrument metadata, stock details, performance snapshot, and the
    current portfolio line for that ISIN when held. Read-only.
    """
    try:
        validated = TickerInput(ticker=ticker)
        return await get_client().get_ticker_details(validated.ticker, include_position=True)
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("get_stock_analysis")
async def get_stock_analysis(ticker: str, include_position: bool = False) -> dict:
    """Return deep fundamental analysis for a stock ISIN.

    Includes company details, annual KPIs, dividend history, and performance.
    Works for any stock, not only portfolio holdings. Requires login. Read-only.

    Args:
        ticker: ISIN (e.g. US0378331005 for Apple).
        include_position: When true, also attach the portfolio line if this ISIN is held.
    """
    try:
        validated = TickerInput(ticker=ticker)
        return await get_client().get_stock_analysis(
            validated.ticker,
            include_position=include_position,
        )
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("get_etf_analysis")
async def get_etf_analysis(ticker: str, include_position: bool = False) -> dict:
    """Return ETF details and portfolio composition for an ISIN.

    Requires login. Read-only.

    Args:
        ticker: ISIN of the ETF (e.g. IE00B4L5Y983 for iShares Core MSCI World).
        include_position: When true, also attach the portfolio line if held.
    """
    try:
        validated = TickerInput(ticker=ticker)
        return await get_client().get_etf_analysis(
            validated.ticker,
            include_position=include_position,
        )
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("get_crypto_analysis")
async def get_crypto_analysis(ticker: str, include_position: bool = False) -> dict:
    """Return crypto asset details for an ISIN.

    Requires login. Read-only.

    Args:
        ticker: ISIN of the crypto instrument.
        include_position: When true, also attach the portfolio line if held.
    """
    try:
        validated = TickerInput(ticker=ticker)
        return await get_client().get_crypto_analysis(
            validated.ticker,
            include_position=include_position,
        )
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("search_instruments")
async def search_instruments(
    query: str,
    instrument_type: str = "stock",
    jurisdiction: str = "DE",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Search Trade Republic for stocks, ETFs, crypto, or derivatives.

    Use this to find ISINs by company name or keyword before fetching details.
    Does not require a logged-in account. Read-only.

    Args:
        query: Search text (e.g. "Apple", "Tesla", "MSCI World").
        instrument_type: One of stock, fund, derivative, crypto.
        jurisdiction: Country filter (DE, AT, FR, …).
        page: Result page, starting at 1.
        page_size: Number of results per page (max 100).
    """
    try:
        validated = SearchInstrumentsInput(
            query=query,
            instrument_type=instrument_type,
            jurisdiction=jurisdiction,
            page=page,
            page_size=page_size,
        )
        return await get_client().search_instruments(
            query=validated.query,
            instrument_type=validated.instrument_type,
            jurisdiction=validated.jurisdiction,
            page=validated.page,
            page_size=validated.page_size,
        )
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("get_price_history")
async def get_price_history(
    ticker: str,
    range: str = "1y",
    exchange: str = "LSX",
) -> dict:
    """Return price history for any ISIN, even if not held in the portfolio.

    Useful for charts and performance context. Does not require login. Read-only.

    Args:
        ticker: ISIN (e.g. US0378331005).
        range: 1d, 5d, 1m, 3m, 1y, or max.
        exchange: Trading venue (default LSX = Lang & Schwarz).
    """
    try:
        validated = PriceHistoryInput(ticker=ticker, range=range, exchange=exchange)
        return await get_client().get_price_history(
            validated.ticker,
            range=validated.range,
            exchange=validated.exchange,
        )
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("get_stock_news")
async def get_stock_news(ticker: str) -> dict:
    """Return recent news articles for an ISIN.

    Works for any instrument, not only portfolio holdings. No login required. Read-only.

    Args:
        ticker: ISIN of the instrument (e.g. US0378331005).
    """
    try:
        validated = TickerInput(ticker=ticker)
        return await get_client().get_stock_news(validated.ticker)
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("get_portfolio_history")
async def get_portfolio_history(range: str = "max") -> dict:
    """Return portfolio value history over time for the logged-in account.

    Useful for performance charts and depot development questions. Requires login. Read-only.

    Args:
        range: Time window — 1d, 5d, 1m, 3m, 1y, or max.
    """
    try:
        validated = PortfolioHistoryInput(range=range)
        return await get_client().get_portfolio_history(validated.range)
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("get_watchlist")
async def get_watchlist() -> dict:
    """Return instruments on the account watchlist.

    Requires login. Read-only.
    """
    try:
        return await get_client().get_watchlist()
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("get_recent_transactions")
async def get_recent_transactions(limit: int = 20, after: str | None = None) -> dict:
    """Return recent account transactions (trades, dividends, deposits, etc.).

    Uses the cash-relevant timeline subset. Requires login. Read-only.

    Args:
        limit: Maximum number of events to return (1–100, default 20).
        after: Optional pagination cursor for older events.
    """
    try:
        validated = TransactionsInput(limit=limit, after=after)
        return await get_client().get_recent_transactions(
            limit=validated.limit,
            after=validated.after,
        )
    except Exception as exc:
        raise_structured(exc)


async def _watchlist_mutation(
    action: str,
    ticker: str,
    confirmed: bool,
    mutate,
    confirm_token: str | None = None,
) -> dict:
    require_write_enabled()
    validated = TickerInput(ticker=ticker)
    client = get_client()
    if not confirmed:
        name = await client.instrument_label(validated.ticker)
        return confirmation_required(action, validated.ticker, instrument_name=name)
    require_confirmation(action, validated.ticker, confirm_token)
    return await mutate(validated.ticker)


@mcp.tool()
@log_tool_call("add_to_watchlist")
async def add_to_watchlist(
    ticker: str,
    confirmed: bool = False,
    confirm_token: str | None = None,
) -> dict:
    """Add an instrument to the Trade Republic watchlist.

    MUTATING: Changes your watchlist only — does not buy or sell.

    Workflow:
    1. Call with confirmed=false (default) → returns confirmation_required,
       a German prompt in 'message', and a one-time confirm_token.
    2. After clear user consent, call again with confirmed=true AND that
       confirm_token to execute. Bare confirmed=true without the token is rejected.

    Requires TR_MCP_WRITE_ENABLED=1 and login credentials.
    """
    try:
        return await _watchlist_mutation(
            "add_to_watchlist",
            ticker,
            confirmed,
            get_client().add_to_watchlist,
            confirm_token=confirm_token,
        )
    except Exception as exc:
        raise_structured(exc)


@mcp.tool()
@log_tool_call("remove_from_watchlist")
async def remove_from_watchlist(
    ticker: str,
    confirmed: bool = False,
    confirm_token: str | None = None,
) -> dict:
    """Remove an instrument from the Trade Republic watchlist.

    MUTATING: Changes your watchlist only — does not buy or sell.

    Workflow:
    1. Call with confirmed=false (default) → returns confirmation_required,
       a German prompt in 'message', and a one-time confirm_token.
    2. After clear user consent, call again with confirmed=true AND that
       confirm_token to execute. Bare confirmed=true without the token is rejected.

    Requires TR_MCP_WRITE_ENABLED=1 and login credentials.
    """
    try:
        return await _watchlist_mutation(
            "remove_from_watchlist",
            ticker,
            confirmed,
            get_client().remove_from_watchlist,
            confirm_token=confirm_token,
        )
    except Exception as exc:
        raise_structured(exc)


if __name__ == "__main__":
    mcp.run(transport="stdio")
