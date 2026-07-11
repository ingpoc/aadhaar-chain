"""Agent runtime policy for AadhaarChain gateway — Cursor SDK only."""
from __future__ import annotations

import functools
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_SHARED_ROOT = _WORKSPACE_ROOT / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from cursor_agent_runtime.policy import (  # noqa: E402
    resolve_runtime_policy as _resolve_cursor_policy,
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
    _resolve_cursor_policy.cache_clear()
