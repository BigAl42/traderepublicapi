"""Structured adapter errors for Hermes / MCP consumers."""

from __future__ import annotations

import json
from typing import Any

from mcp_write import (
    ConfirmationError,
    TradingToolsDisabledError,
    WriteToolsDisabledError,
)
from redact import redact_secrets
from session import ErrorKind
from tr_client import TradeRepublicClientError


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Normalize exceptions into a stable JSON-serializable error object."""
    if isinstance(exc, WriteToolsDisabledError):
        return {
            "status": "error",
            "code": "writes_disabled",
            "message": redact_secrets(str(exc)),
            "retryable": False,
            "retry_after_seconds": None,
            "guidance": (
                "Set TR_MCP_WRITE_ENABLED=1 only when watchlist mutations are intended. "
                "Default production is writes off."
            ),
        }
    if isinstance(exc, TradingToolsDisabledError):
        return {
            "status": "error",
            "code": "trading_disabled",
            "message": redact_secrets(str(exc)),
            "retryable": False,
            "retry_after_seconds": None,
            "guidance": (
                "Set TR_MCP_TRADING_ENABLED=1 only when real-money orders are intended. "
                "There is no dry-run. Default production is trading off."
            ),
        }
    if isinstance(exc, ConfirmationError):
        return {
            "status": "error",
            "code": "confirmation_required_or_invalid",
            "message": redact_secrets(str(exc)),
            "retryable": True,
            "retry_after_seconds": None,
            "guidance": (
                "Call the write tool with confirmed=false to obtain a confirm_token, "
                "ask the user, then retry with confirmed=true and that token."
            ),
        }
    if isinstance(exc, TradeRepublicClientError):
        kind_value = getattr(exc.kind, "value", None) or ErrorKind.UNKNOWN.value
        retry_after = exc.retry_after_seconds
        if exc.guidance:
            guidance = exc.guidance
        elif kind_value == ErrorKind.RATE_LIMITED.value:
            guidance = (
                "Auth circuit is open or TR rate-limited login. Do not trigger push login. "
                "Call get_adapter_status, wait retry_after_seconds. Do NOT run "
                "check_login.py / trigger_login.py / custom Python from the agent."
            )
        elif kind_value == ErrorKind.AWAITING_PUSH_CONFIRM.value:
            guidance = (
                "A Trade Republic app push was already sent. Ask the user to confirm it, "
                "then call the SAME MCP tool again (not a login script). Do NOT start a "
                "new login or run check_login.py / trigger_login.py / tr.login()."
            )
        elif kind_value == ErrorKind.AWAITING_AUTHENTICATOR.value:
            guidance = (
                "Ask the user for their Trade Republic authenticator code, then retry the "
                "SAME MCP tool (session renew runs automatically). Do not write login scripts."
            )
        elif kind_value == ErrorKind.LOGIN_REQUIRED.value:
            guidance = (
                "Trade Republic session is cold and automatic renew failed. Retry the same "
                "MCP tool after the operator sets TR_TOKEN or TR_PHONE/TR_PIN. Do NOT run "
                "check_login.py, trigger_login.py, tr.login(), or switch providers."
            )
        elif kind_value == ErrorKind.SESSION_EXPIRED.value:
            guidance = (
                "Session expired mid-call. Retry the same MCP tool once. Do not write "
                "login scripts or change providers."
            )
        elif exc.retryable:
            guidance = (
                "Transient session/network issue. Wait briefly, call get_adapter_status, "
                "retry once — never rapid re-login loops."
            )
        else:
            guidance = "Fix configuration (credentials, client version) before retrying."
        payload: dict[str, Any] = {
            "status": "error",
            "code": kind_value,
            "message": redact_secrets(str(exc)),
            "retryable": bool(exc.retryable),
            "retry_after_seconds": retry_after,
            "guidance": guidance,
        }
        if kind_value == ErrorKind.AWAITING_PUSH_CONFIRM.value:
            payload["status"] = "awaiting_push_confirm"
        elif kind_value == ErrorKind.AWAITING_AUTHENTICATOR.value:
            payload["status"] = "awaiting_authenticator"
        return payload
    return {
        "status": "error",
        "code": "unexpected",
        "message": redact_secrets(str(exc)),
        "retryable": False,
        "retry_after_seconds": None,
        "guidance": "Unexpected adapter error. Inspect logs; do not spam login.",
    }


def raise_structured(exc: BaseException) -> None:
    """Raise RuntimeError whose message is a JSON error payload (for MCP clients)."""
    payload = error_payload(exc)
    raise RuntimeError(json.dumps(payload, ensure_ascii=False)) from exc
