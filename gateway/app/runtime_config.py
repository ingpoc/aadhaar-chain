"""Claude Agent SDK runtime policy for aadhaar-chain internal agents."""
from __future__ import annotations

import functools
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from config import settings

AgentAuthMode = Literal["api_key", "local_cli", "bedrock", "vertex", "azure", "unavailable"]


@dataclass(frozen=True)
class AgentRuntimePolicy:
    runtime_available: bool
    auth_mode: AgentAuthMode
    model: str
    blocked_reason: Optional[str]
    claude_code_executable_path: Optional[str]


def _allow_deployed_cli_auth() -> bool:
    return bool(settings.claude_agent_allow_deployed_cli_auth)


def _find_claude_code_executable() -> Optional[str]:
    if settings.claude_code_executable:
        return settings.claude_code_executable if Path(settings.claude_code_executable).exists() else None

    explicit = os.getenv("CLAUDE_CODE_EXECUTABLE") or os.getenv("CLAUDE_CODE_PATH")
    if explicit:
        return explicit if Path(explicit).exists() else None

    discovered = shutil.which("claude")
    if discovered:
        return discovered

    fallback = Path.home() / ".claude" / "local" / "claude"
    if fallback.exists():
        return str(fallback)

    return None


@functools.lru_cache(maxsize=1)
def resolve_runtime_policy() -> AgentRuntimePolicy:
    requested_auth_mode = (settings.claude_agent_auth_mode or "auto").strip().lower()
    if requested_auth_mode not in {"auto", "api_key", "local_cli", "bedrock", "vertex", "azure"}:
        requested_auth_mode = "auto"

    has_api_key = bool(settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY"))
    cli_path = _find_claude_code_executable()

    if requested_auth_mode in {"bedrock", "vertex", "azure"}:
        return AgentRuntimePolicy(
            runtime_available=True,
            auth_mode=requested_auth_mode,  # type: ignore[arg-type]
            model=settings.claude_agent_model,
            blocked_reason=None,
            claude_code_executable_path=cli_path,
        )

    if requested_auth_mode == "api_key":
        if has_api_key:
            return AgentRuntimePolicy(
                runtime_available=True,
                auth_mode="api_key",
                model=settings.claude_agent_model,
                blocked_reason=None,
                claude_code_executable_path=cli_path,
            )
        return AgentRuntimePolicy(
            runtime_available=False,
            auth_mode="unavailable",
            model=settings.claude_agent_model,
            blocked_reason="ANTHROPIC_API_KEY is required for the configured AadhaarChain runtime mode.",
            claude_code_executable_path=cli_path,
        )

    if requested_auth_mode == "auto" and has_api_key:
        return AgentRuntimePolicy(
            runtime_available=True,
            auth_mode="api_key",
            model=settings.claude_agent_model,
            blocked_reason=None,
            claude_code_executable_path=cli_path,
        )

    if requested_auth_mode in {"local_cli", "auto"}:
        if not settings.claude_agent_allow_local_cli_auth:
            return AgentRuntimePolicy(
                runtime_available=False,
                auth_mode="unavailable",
                model=settings.claude_agent_model,
                blocked_reason="Claude Code CLI auth is disabled for this AadhaarChain runtime.",
                claude_code_executable_path=cli_path,
            )
        if os.getenv("ENV", "development") == "production" and not _allow_deployed_cli_auth():
            return AgentRuntimePolicy(
                runtime_available=False,
                auth_mode="unavailable",
                model=settings.claude_agent_model,
                blocked_reason=(
                    "Claude Code CLI auth is restricted to local/non-production AadhaarChain runtimes unless "
                    "CLAUDE_AGENT_ALLOW_DEPLOYED_CLI_AUTH=true is set on the gateway host."
                ),
                claude_code_executable_path=cli_path,
            )
        if not cli_path:
            return AgentRuntimePolicy(
                runtime_available=False,
                auth_mode="unavailable",
                model=settings.claude_agent_model,
                blocked_reason="Claude Code CLI auth requires the local `claude` executable to be installed or CLAUDE_CODE_EXECUTABLE to be set.",
                claude_code_executable_path=None,
            )
        return AgentRuntimePolicy(
            runtime_available=True,
            auth_mode="local_cli",
            model=settings.claude_agent_model,
            blocked_reason=None,
            claude_code_executable_path=cli_path,
        )

    return AgentRuntimePolicy(
        runtime_available=False,
        auth_mode="unavailable",
        model=settings.claude_agent_model,
        blocked_reason="No supported Claude Agent SDK auth mode is configured for AadhaarChain.",
        claude_code_executable_path=cli_path,
    )
