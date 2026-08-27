"""Guards and confirmation helpers for mutating MCP tools.

Confirmation is bound: a one-time confirm_token must be issued first and
redeemed within a TTL. Bare ``confirmed=true`` without a valid token is rejected.

Trading tokens additionally bind side/size/price (and related fields) so a
token issued for one order cannot execute a different one.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any


class WriteToolsDisabledError(Exception):
    """Raised when a mutating tool is called without TR_MCP_WRITE_ENABLED."""


class TradingToolsDisabledError(Exception):
    """Raised when a trading tool is called without TR_MCP_TRADING_ENABLED."""


class ConfirmationError(Exception):
    """Raised when mutating confirmation is missing, expired, or mismatched."""


def write_enabled() -> bool:
    return os.getenv("TR_MCP_WRITE_ENABLED", "").strip().lower() in ("1", "true", "yes")


def trading_enabled() -> bool:
    return os.getenv("TR_MCP_TRADING_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def require_write_enabled() -> None:
    if not write_enabled():
        raise WriteToolsDisabledError(
            "Write tools are disabled. Set TR_MCP_WRITE_ENABLED=1 in the environment "
            "to allow watchlist changes."
        )


def require_trading_enabled() -> None:
    if not trading_enabled():
        raise TradingToolsDisabledError(
            "Trading tools are disabled. Set TR_MCP_TRADING_ENABLED=1 in the "
            "environment to allow real-money limit/stop-market orders and cancels. "
            "There is no dry-run."
        )


def _confirm_ttl_seconds() -> int:
    raw = os.getenv("TR_MCP_CONFIRM_TTL_SECONDS", "300")
    try:
        return max(30, int(raw))
    except ValueError:
        return 300


def _confirm_store_path() -> Path:
    override = os.getenv("TR_MCP_CONFIRM_STORE")
    if override:
        return Path(override)
    cookies = os.getenv("TR_COOKIES_FILE", "tr_cookies.txt")
    return Path(cookies).with_name(Path(cookies).name + ".confirmations.json")


def normalize_binding(binding: dict[str, Any] | None) -> dict[str, str]:
    """Stable string map for confirm_token binding comparison."""
    if not binding:
        return {}
    out: dict[str, str] = {}
    for key in sorted(binding.keys()):
        value = binding[key]
        if value is None:
            continue
        if isinstance(value, bool):
            out[str(key)] = "true" if value else "false"
        elif isinstance(value, float):
            out[str(key)] = format(value, ".8g")
        else:
            out[str(key)] = str(value)
    return out


class ConfirmationStore:
    """File-backed one-time confirmation tokens (survives MCP respawns within TTL)."""

    def __init__(self, path: Path | None = None):
        self.path = path or _confirm_store_path()
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"tokens": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "tokens" not in data:
                return {"tokens": {}}
            return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"tokens": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _purge_expired(self, data: dict[str, Any], now: float) -> None:
        tokens = data.get("tokens") or {}
        data["tokens"] = {
            key: value
            for key, value in tokens.items()
            if float(value.get("expires_at", 0)) > now
        }

    def issue(
        self,
        action: str,
        ticker: str,
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        ttl = _confirm_ttl_seconds()
        token = secrets.token_urlsafe(24)
        normalized = normalize_binding(binding)
        with self._lock:
            data = self._load()
            self._purge_expired(data, now)
            data["tokens"][token] = {
                "action": action,
                "ticker": ticker,
                "binding": normalized,
                "expires_at": now + ttl,
                "created_at": now,
            }
            self._save(data)
        return {
            "confirm_token": token,
            "expires_in_seconds": ttl,
            "expires_at": int(now + ttl),
        }

    def redeem(
        self,
        action: str,
        ticker: str,
        confirm_token: str | None,
        binding: dict[str, Any] | None = None,
    ) -> None:
        if not confirm_token:
            raise ConfirmationError(
                "Missing confirm_token. Call the tool first with confirmed=false, "
                "show the user the message, then retry with confirmed=true AND the "
                "confirm_token from the previous response."
            )
        now = time.time()
        expected_binding = normalize_binding(binding)
        with self._lock:
            data = self._load()
            self._purge_expired(data, now)
            entry = (data.get("tokens") or {}).get(confirm_token)
            if entry is None:
                raise ConfirmationError(
                    "Invalid or expired confirm_token. Request a new confirmation "
                    "(confirmed=false) and ask the user again."
                )
            stored_binding = normalize_binding(entry.get("binding") or {})
            if (
                entry.get("action") != action
                or entry.get("ticker") != ticker
                or stored_binding != expected_binding
            ):
                raise ConfirmationError(
                    "confirm_token does not match this action/ticker/parameters. "
                    "Request a fresh confirmation for the intended change."
                )
            # One-time use.
            del data["tokens"][confirm_token]
            self._save(data)


_STORE = ConfirmationStore()


def confirmation_required(
    action: str,
    ticker: str,
    *,
    instrument_name: str | None = None,
    binding: dict[str, Any] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Return a structured payload asking the user to confirm before mutating."""
    label = instrument_name or ticker
    if message is None:
        if action == "add_to_watchlist":
            message = (
                f"Möchtest du {label} ({ticker}) zur Trade-Republic-Watchlist hinzufügen?"
            )
        elif action == "remove_from_watchlist":
            message = (
                f"Möchtest du {label} ({ticker}) von der Trade-Republic-Watchlist entfernen?"
            )
        else:
            message = f"Möchtest du die Aktion '{action}' für {label} ({ticker}) ausführen?"

    issued = _STORE.issue(action, ticker, binding=binding)
    payload: dict[str, Any] = {
        "status": "confirmation_required",
        "action": action,
        "ticker": ticker,
        "instrument_name": instrument_name,
        "message": message,
        "confirm_token": issued["confirm_token"],
        "expires_in_seconds": issued["expires_in_seconds"],
        "instructions": (
            "Zeige dem Nutzer die Nachricht in 'message' und frage explizit nach Zustimmung. "
            "Führe die Änderung nur aus, wenn der Nutzer klar zustimmt — dann rufe dieses "
            "Tool erneut mit confirmed=true UND dem confirm_token aus dieser Antwort auf. "
            "Parameter dürfen zwischen Preview und Ausführung nicht geändert werden."
        ),
    }
    if binding:
        payload["binding"] = normalize_binding(binding)
    return payload


def require_confirmation(
    action: str,
    ticker: str,
    confirm_token: str | None,
    binding: dict[str, Any] | None = None,
) -> None:
    _STORE.redeem(action, ticker, confirm_token, binding=binding)


def order_confirmation_message(
    *,
    action: str,
    ticker: str,
    instrument_name: str | None = None,
    order_type: str | None = None,
    size: float | None = None,
    limit: float | None = None,
    stop: float | None = None,
    expiry: str | None = None,
    exchange: str | None = None,
    order_id: str | None = None,
) -> str:
    """German user-facing prompt for trading confirmations."""
    label = instrument_name or ticker
    money = "ECHTES GELD — es gibt keinen Dry-Run."
    if action == "place_limit_order":
        return (
            f"Möchtest du LIMIT {order_type.upper()} {size} Stück von {label} ({ticker}) "
            f"zu Limit {limit} EUR an {exchange} (Gültigkeit {expiry}) ausführen? {money}"
        )
    if action == "place_stop_market_order":
        side_label = "Stop-Loss" if (order_type or "").lower() == "sell" else "Stop-Buy"
        return (
            f"Möchtest du STOP-MARKET {order_type.upper()} ({side_label}) {size} Stück von "
            f"{label} ({ticker}) bei Stop {stop} EUR an {exchange} "
            f"(Gültigkeit {expiry}) ausführen? {money}"
        )
    if action == "cancel_order":
        return f"Möchtest du die Order {order_id} stornieren? {money}"
    return f"Möchtest du die Trading-Aktion '{action}' ausführen? {money}"
