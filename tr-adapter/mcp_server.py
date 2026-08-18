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

    try:
        Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        logger.warning("Could not open log file %s; logging to stderr", LOG_PATH)

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
                        exc,
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
                LOGGER.info("tool=%s status=error timestamp=%s detail=%s", name, started, exc)
                raise

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _format_error(exc: Exception) -> str:
    if isinstance(exc, TradeRepublicClientError):
        prefix = "Trade Republic session problem" if exc.retryable else "Trade Republic configuration problem"
        return f"{prefix}: {exc}"
    return f"Unexpected adapter error: {exc}"


@mcp.tool()
@log_tool_call("get_account_summary")
async def get_account_summary() -> dict:
    """Return account cash balances, buying power, and portfolio status.

    Use this for a high-level financial overview: total cash, funds available
    to buy securities, cash available for payout, and overall portfolio status.
    Read-only; no orders or transfers.
    """
    try:
        return get_client().get_balance_info()
    except Exception as exc:
        raise RuntimeError(_format_error(exc)) from exc


@mcp.tool()
@log_tool_call("list_active_positions")
async def list_active_positions() -> list:
    """List all currently held portfolio positions.

    Each item includes ticker (ISIN), name, quantity, average buy-in,
    instrument type, status, and profit/loss when provided by Trade Republic.
    Read-only.
    """
    try:
        return get_client().get_holdings()
    except Exception as exc:
        raise RuntimeError(_format_error(exc)) from exc


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
        return get_client().get_ticker_details(validated.ticker)
    except Exception as exc:
        raise RuntimeError(_format_error(exc)) from exc


if __name__ == "__main__":
    mcp.run(transport="stdio")
