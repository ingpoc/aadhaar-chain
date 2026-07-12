"""Agent runtime policy for AadhaarChain gateway — Cursor SDK only."""
from __future__ import annotations

import functools
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

_HERE = Path(__file__).resolve()
# gateway/app/runtime_config.py → parents[1] = gateway root (/app in Docker).
# Monorepo checkout may also expose workspace/shared at parents[3].
_GATEWAY_ROOT = _HERE.parents[1]
_WORKSPACE_ROOT = _GATEWAY_ROOT
if len(_HERE.parents) > 3:
    candidate = _HERE.parents[3]
    if (candidate / "shared" / "cursor_agent_runtime").is_dir():
        _WORKSPACE_ROOT = candidate
_SHARED_ROOT = _WORKSPACE_ROOT / "shared"
if _SHARED_ROOT.is_dir() and str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

try:
    from cursor_agent_runtime.policy import (  # noqa: E402
        resolve_runtime_policy as _resolve_cursor_policy,
    )
except ImportError:  # Docker gateway image has no workspace/shared — use env + cursor-sdk
    import importlib.util
    import os

    @dataclass(frozen=True)
    class _InlineCursorPolicy:
        runtime_available: bool
        auth_mode: str
        model: str
        blocked_reason: Optional[str]

    @functools.lru_cache(maxsize=1)
    def _resolve_cursor_policy():  # type: ignore[misc]
        model = (os.getenv("CURSOR_AGENT_MODEL") or "composer-2.5").strip() or "composer-2.5"
        has_sdk = importlib.util.find_spec("cursor_sdk") is not None
        has_key = bool((os.getenv("CURSOR_API_KEY") or "").strip())
        if not has_sdk:
            return _InlineCursorPolicy(
                runtime_available=False,
                auth_mode="unavailable",
                model=model,
                blocked_reason="cursor-sdk is not installed in the gateway image",
            )
        if not has_key:
            return _InlineCursorPolicy(
                runtime_available=False,
                auth_mode="unavailable",
                model=model,
                blocked_reason="CURSOR_API_KEY is required on the gateway",
            )
        return _InlineCursorPolicy(
            runtime_available=True,
            auth_mode="cursor_api_key",
            model=model,
            blocked_reason=None,
        )

    def _clear_cursor_policy_cache() -> None:
        _resolve_cursor_policy.cache_clear()
else:
    _clear_cursor_policy_cache: Callable[[], None] = (
        _resolve_cursor_policy.cache_clear  # type: ignore[attr-defined]
    )

AgentAuthMode = Literal["api_key", "unavailable", "cursor_api_key"]


@dataclass(frozen=True)
class AgentRuntimePolicy:
    runtime_available: bool
    auth_mode: AgentAuthMode
    model: str
    blocked_reason: Optional[str]
    provider: Literal["cursor", "none"] = "none"


@functools.lru_cache(maxsize=1)
def resolve_runtime_policy() -> AgentRuntimePolicy:
    cursor = _resolve_cursor_policy()
    auth_mode: AgentAuthMode = (
        "api_key" if cursor.auth_mode == "cursor_api_key" else "unavailable"
    )
    return AgentRuntimePolicy(
        runtime_available=cursor.runtime_available,
        auth_mode=auth_mode,
        model=cursor.model,
        blocked_reason=cursor.blocked_reason,
        provider="cursor" if cursor.runtime_available else "none",
    )


def clear_runtime_policy_cache() -> None:
    resolve_runtime_policy.cache_clear()
    _clear_cursor_policy_cache()
