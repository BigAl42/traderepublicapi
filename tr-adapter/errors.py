"""Structured adapter errors for Hermes / MCP consumers."""

from __future__ import annotations

import json
from typing import Any

from mcp_write import ConfirmationError, WriteToolsDisabledError
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
        if kind_value == ErrorKind.RATE_LIMITED.value:
            guidance = (
                "Auth circuit is open or TR rate-limited login. Do not trigger push login. "
                "Call get_adapter_status, wait retry_after_seconds, then resume from "
                "TR_TOKEN / cookies via check_login.py if needed."
            )
        elif kind_value == ErrorKind.LOGIN_REQUIRED.value:
            guidance = (
                "No warm session. Warm cookies offline with check_login.py or set TR_TOKEN. "
                "Keep TR_MCP_ALLOW_INTERACTIVE_LOGIN=0 in production."
            )
        elif exc.retryable:
            guidance = (
                "Transient session/network issue. Wait briefly, call get_adapter_status, "
                "retry once — never rapid re-login loops."
            )
        else:
            guidance = "Fix configuration (credentials, client version) before retrying."
        return {
            "status": "error",
            "code": kind_value,
            "message": redact_secrets(str(exc)),
            "retryable": bool(exc.retryable),
            "retry_after_seconds": retry_after,
            "guidance": guidance,
        }
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
