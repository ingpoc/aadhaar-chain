"""Gateway import boundary for the shared verified runtime outcome contract.

The Render image is built with ``aadharchain/gateway`` as its Docker context,
so it cannot import the monorepo's ``shared`` directory. Keep the compact
fallback aligned with ``shared/cursor_agent_runtime/outcome.py``.
"""
from __future__ import annotations

try:
    from cursor_agent_runtime.outcome import (  # type: ignore[import-not-found]
        RuntimeOutcomeError,
        completed_tool_names,
        parse_verified_runtime_outcome,
    )
except ImportError:
    import json
    from dataclasses import dataclass
    from typing import Any, Iterable, Mapping

    class RuntimeOutcomeError(ValueError):
        """The agent response did not prove completion."""

    @dataclass(frozen=True)
    class _VerifiedRuntimeOutcome:
        summary: str
        executed_tools: tuple[str, ...]
        postcondition_evidence: str

        def as_dict(self) -> dict[str, Any]:
            return {
                "status": "completed",
                "summary": self.summary,
                "executed_tools": list(self.executed_tools),
                "postcondition": {
                    "verified": True,
                    "evidence": self.postcondition_evidence,
                },
            }

    def completed_tool_names(messages: Iterable[Any]) -> tuple[str, ...]:
        names: list[str] = []
        for message in messages:
            if (
                getattr(message, "type", None) == "tool_call"
                and getattr(message, "status", None) == "completed"
            ):
                name = getattr(message, "name", None)
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
        return tuple(names)

    def parse_verified_runtime_outcome(
        content: str,
        *,
        observed_completed_tools: Iterable[str],
    ) -> _VerifiedRuntimeOutcome:
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeOutcomeError(
                "The runtime returned narrative text without verifiable completion evidence."
            ) from exc
        if not isinstance(payload, Mapping) or payload.get("status") != "completed":
            raise RuntimeOutcomeError("The runtime did not report a completed structured outcome.")
        summary = payload.get("summary")
        declared_tools = payload.get("executed_tools")
        postcondition = payload.get("postcondition")
        evidence = postcondition.get("evidence") if isinstance(postcondition, Mapping) else None
        if not isinstance(summary, str) or not summary.strip():
            raise RuntimeOutcomeError("The runtime completion summary is missing.")
        if (
            not isinstance(declared_tools, list)
            or not declared_tools
            or not all(isinstance(name, str) and name.strip() for name in declared_tools)
        ):
            raise RuntimeOutcomeError("The runtime did not declare valid executed tool evidence.")
        observed = {name.strip() for name in observed_completed_tools if name.strip()}
        declared = tuple(dict.fromkeys(name.strip() for name in declared_tools))
        if not observed or any(name not in observed for name in declared):
            raise RuntimeOutcomeError(
                "The runtime completion was not backed by completed SDK tool calls."
            )
        if not isinstance(postcondition, Mapping) or postcondition.get("verified") is not True:
            raise RuntimeOutcomeError("The runtime did not verify the requested postcondition.")
        if not isinstance(evidence, str) or not evidence.strip():
            raise RuntimeOutcomeError("The runtime postcondition evidence is missing.")
        return _VerifiedRuntimeOutcome(
            summary=summary.strip()[:280],
            executed_tools=declared,
            postcondition_evidence=evidence.strip()[:500],
        )


__all__ = [
    "RuntimeOutcomeError",
    "completed_tool_names",
    "parse_verified_runtime_outcome",
]
