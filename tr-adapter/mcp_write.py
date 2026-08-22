"""Guards and confirmation helpers for mutating MCP tools."""

from __future__ import annotations

import os
from typing import Any


class WriteToolsDisabledError(Exception):
    """Raised when a mutating tool is called without TR_MCP_WRITE_ENABLED."""


def write_enabled() -> bool:
    return os.getenv("TR_MCP_WRITE_ENABLED", "").strip().lower() in ("1", "true", "yes")


def require_write_enabled() -> None:
    if not write_enabled():
        raise WriteToolsDisabledError(
            "Write tools are disabled. Set TR_MCP_WRITE_ENABLED=1 in the environment "
            "to allow watchlist changes."
        )


def confirmation_required(
    action: str,
    ticker: str,
    *,
    instrument_name: str | None = None,
) -> dict[str, Any]:
    """Return a structured payload asking the user to confirm before mutating."""
    label = instrument_name or ticker
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

    return {
        "status": "confirmation_required",
        "action": action,
        "ticker": ticker,
        "instrument_name": instrument_name,
        "message": message,
        "instructions": (
            "Zeige dem Nutzer die Nachricht in 'message' und frage explizit nach Zustimmung. "
            "Führe die Änderung nur aus, wenn der Nutzer klar zustimmt — dann rufe dieses "
            "Tool erneut mit confirmed=true auf."
        ),
    }
