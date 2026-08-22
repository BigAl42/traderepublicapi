"""Trade Republic MCP server entrypoint (Docker / repo root).

Canonical implementation lives in ``tr-adapter/mcp_server.py``. This module
loads that file under a stable name so Docker and Hermes share one code path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parent
_ADAPTER_DIR = _ROOT / "tr-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "tr_adapter_mcp_server",
    _ADAPTER_DIR / "mcp_server.py",
)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Unable to load tr-adapter/mcp_server.py")

_IMPL = importlib.util.module_from_spec(_SPEC)
sys.modules["tr_adapter_mcp_server"] = _IMPL
_SPEC.loader.exec_module(_IMPL)


class _RootMcpModule(ModuleType):
    """Proxy so ``mcp_server._client = …`` updates the adapter implementation."""

    def __getattr__(self, name: str):  # noqa: D105
        return getattr(_IMPL, name)

    def __setattr__(self, name: str, value: object) -> None:  # noqa: D105
        if name in {"_IMPL", "_SPEC", "_ADAPTER_DIR", "_ROOT"}:
            return super().__setattr__(name, value)
        if hasattr(_IMPL, name) or name == "_client":
            setattr(_IMPL, name, value)
            return None
        return super().__setattr__(name, value)


_proxy = _RootMcpModule(__name__)
_proxy.__dict__.update({k: v for k, v in globals().items() if k != "__name__"})
# Keep a local handle for __main__ without going through __getattr__ recursion.
_proxy.__dict__["_IMPL"] = _IMPL
sys.modules[__name__] = _proxy

# Re-export common names for static analyzers / ``from mcp_server import mcp``.
mcp = _IMPL.mcp
get_client = _IMPL.get_client
TickerInput = _IMPL.TickerInput

if __name__ == "__main__":
    _IMPL.mcp.run(transport="stdio")
