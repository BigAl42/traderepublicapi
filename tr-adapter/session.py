"""Professional Trade Republic session management for the MCP adapter.

Goals:
- Cookie-first, login-last (avoid push-login storms)
- Classify auth errors (rate-limit vs expired vs network)
- Persist a circuit breaker across process restarts (Hermes respawns MCP often)
- Soft-refresh sessions before mutating calls
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from redact import redact_secrets

LOGGER = logging.getLogger("tr_adapter.session")


class ErrorKind(str, Enum):
    RATE_LIMITED = "rate_limited"
    SESSION_EXPIRED = "session_expired"
    LOGIN_REQUIRED = "login_required"
    AWAITING_PUSH_CONFIRM = "awaiting_push_confirm"
    AWAITING_AUTHENTICATOR = "awaiting_authenticator"
    AUTH_FAILED = "auth_failed"
    NETWORK = "network"
    SERVER = "server"
    CONFIG = "config"
    UNKNOWN = "unknown"


@dataclass
class ClassifiedError:
    kind: ErrorKind
    message: str
    retryable: bool
    retry_after_seconds: int | None = None
    allow_relogin: bool = False


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def classify_auth_error(exc: Exception) -> ClassifiedError:
    """Map raw exceptions into actionable session errors."""
    message = str(exc)
    lower = message.lower()

    if (
        "too many" in lower
        or "too_many" in lower
        or "rate limit" in lower
        or "429" in lower
    ):
        cooldown = _env_int("TR_AUTH_COOLDOWN_SECONDS", 900)
        return ClassifiedError(
            kind=ErrorKind.RATE_LIMITED,
            message=(
                "Trade Republic rate-limited authentication (too many attempts). "
                f"Do not retry login for at least {cooldown // 60} minutes. "
                "Keep using an existing TR_TOKEN / cookie file if available."
            ),
            retryable=False,
            retry_after_seconds=cooldown,
            allow_relogin=False,
        )

    if "not confirmed in time" in lower or "process_gone" in lower:
        return ClassifiedError(
            kind=ErrorKind.AUTH_FAILED,
            message=(
                "Login was not confirmed in the Trade Republic app in time. "
                "Retry later and approve the push notification — avoid rapid repeats."
            ),
            retryable=True,
            retry_after_seconds=60,
            allow_relogin=True,
        )

    if "client_version_outdated" in lower or "client version" in lower:
        return ClassifiedError(
            kind=ErrorKind.CONFIG,
            message=(
                "Trade Republic rejected this client version. "
                "Update TR_APP_VERSION / headers and try again later."
            ),
            retryable=False,
            allow_relogin=False,
        )

    if (
        "session" in lower
        or "401" in lower
        or "unauthorized" in lower
        or "login failed" in lower
        or "expired" in lower
    ):
        return ClassifiedError(
            kind=ErrorKind.SESSION_EXPIRED,
            message=(
                "Trade Republic session expired or invalid. "
                "Resume from cookies/TR_TOKEN if possible; avoid immediate re-login loops."
            ),
            retryable=True,
            allow_relogin=True,
        )

    if "connection error" in lower or "timeout" in lower or "timed out" in lower:
        return ClassifiedError(
            kind=ErrorKind.NETWORK,
            message="Could not reach Trade Republic (network or server issue). Try again later.",
            retryable=True,
            retry_after_seconds=30,
            allow_relogin=False,
        )

    # Server websocket error states often mean subscription/token expiry.
    type_name = type(exc).__name__.lower()
    if "servererror" in type_name or "error state" in lower:
        return ClassifiedError(
            kind=ErrorKind.SERVER,
            message=(
                "Trade Republic websocket returned an error "
                "(subscription or session may have expired). Retry once with a warm session."
            ),
            retryable=True,
            allow_relogin=False,
        )

    return ClassifiedError(
        kind=ErrorKind.UNKNOWN,
        message=f"Trade Republic API error: {redact_secrets(message)}",
        retryable=True,
        allow_relogin=False,
    )


def adapter_dir() -> Path:
    """Directory containing the tr-adapter package (stable across Hermes cwd changes)."""
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Root for relative cookie / sidecar paths.

    Override with ``TR_ADAPTER_DATA_DIR`` (absolute or ~). Default: adapter package dir
    so Hermes process respawns with a different cwd still share the same files.
    """
    override = os.getenv("TR_ADAPTER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return adapter_dir()


def resolve_runtime_path(path: str | Path) -> Path:
    """Resolve a runtime file path; relative paths anchor to ``data_dir()``."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (data_dir() / candidate).resolve()


def circuit_state_path_for_cookies(cookies_file: str | Path) -> Path:
    """Auth-circuit JSON next to the cookies file (both resolved to absolute paths)."""
    path = resolve_runtime_path(cookies_file)
    return path.with_name(path.name + ".auth_circuit.json")


def login_process_path_for_cookies(cookies_file: str | Path) -> Path:
    """In-flight web-login process id next to the cookies file (survives MCP respawns)."""
    path = resolve_runtime_path(cookies_file)
    return path.with_name(path.name + ".login_process.json")


def load_login_process(path: Path) -> dict[str, Any] | None:
    """Return persisted push-login process if present and not expired."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    process_id = data.get("process_id")
    if not process_id or not isinstance(process_id, str):
        return None
    expires_at = data.get("expires_at")
    now = time.time()
    if isinstance(expires_at, (int, float)):
        # TR often sends ms epoch.
        exp = float(expires_at) / 1000.0 if expires_at > 1e11 else float(expires_at)
        if exp <= now:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
    return {
        "process_id": process_id,
        "expires_at": expires_at,
        "created_at": data.get("created_at"),
    }


def save_login_process(
    path: Path,
    *,
    process_id: str,
    expires_at: Any = None,
) -> None:
    """Persist in-flight login process so auto-recover can poll after MCP respawn."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "process_id": process_id,
        "expires_at": expires_at,
        "created_at": time.time(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_login_process(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class AuthCircuitBreaker:
    """File-backed circuit breaker shared across MCP process restarts."""

    def __init__(
        self,
        state_path: Path,
        *,
        failure_threshold: int | None = None,
        cooldown_seconds: int | None = None,
    ):
        # Always absolute so fcntl lock + state survive cwd changes across respawns.
        self.state_path = resolve_runtime_path(state_path)
        self.failure_threshold = failure_threshold or _env_int("TR_AUTH_MAX_FAILURES", 3)
        self.cooldown_seconds = cooldown_seconds or _env_int("TR_AUTH_COOLDOWN_SECONDS", 900)
        self._lock = threading.Lock()
        self._state = self._load()

    def _default_state(self) -> dict[str, Any]:
        return {
            "open_until": 0.0,
            "failure_count": 0,
            "last_failure_at": 0.0,
            "last_kind": None,
            "last_message": None,
        }

    def _load(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return self._default_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = self._default_state()
            state.update({k: data.get(k, state[k]) for k in state})
            return state
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return self._default_state()

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
            try:
                os.chmod(self.state_path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            LOGGER.warning("Could not persist auth circuit state: %s", exc)

    @contextmanager
    def _cross_process_lock(self, *, persist: bool = True) -> Iterator[None]:
        """Thread lock + fcntl file lock so Hermes respawns share circuit state safely."""
        with self._lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = Path(str(self.state_path) + ".lock")
            with open(lock_path, "a+", encoding="utf-8") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    self._state = self._load()
                    yield
                    if persist:
                        self._save()
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    def remaining_cooldown_seconds(self) -> int:
        with self._cross_process_lock(persist=False):
            open_until = float(self._state.get("open_until") or 0)
            return max(0, int(open_until - time.time()))

    def is_open(self) -> bool:
        return self.remaining_cooldown_seconds() > 0

    def guard(self) -> None:
        remaining = self.remaining_cooldown_seconds()
        if remaining <= 0:
            return
        minutes = max(1, (remaining + 59) // 60)
        raise SessionBlockedError(
            (
                "Authentication circuit is open after repeated Trade Republic auth failures "
                f"(likely rate limiting). Wait ~{minutes} minute(s) before trying again. "
                "Do not trigger new login pushes."
            ),
            kind=ErrorKind.RATE_LIMITED,
            retry_after_seconds=remaining,
        )

    def record_success(self) -> None:
        with self._cross_process_lock():
            self._state = self._default_state()

    def record_failure(self, classified: ClassifiedError) -> None:
        with self._cross_process_lock():
            now = time.time()
            self._state["failure_count"] = int(self._state.get("failure_count") or 0) + 1
            self._state["last_failure_at"] = now
            self._state["last_kind"] = classified.kind.value
            self._state["last_message"] = redact_secrets(classified.message)

            open_now = classified.kind == ErrorKind.RATE_LIMITED
            open_now = open_now or self._state["failure_count"] >= self.failure_threshold
            if open_now:
                cooldown = classified.retry_after_seconds or self.cooldown_seconds
                self._state["open_until"] = max(
                    float(self._state.get("open_until") or 0),
                    now + cooldown,
                )
                LOGGER.warning(
                    "Auth circuit opened for %ss after %s (failures=%s)",
                    cooldown,
                    classified.kind.value,
                    self._state["failure_count"],
                )


class SessionBlockedError(Exception):
    """Raised when auth must not proceed (circuit open / rate limited)."""

    def __init__(
        self,
        message: str,
        *,
        kind: ErrorKind = ErrorKind.RATE_LIMITED,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds
        self.retryable = False
