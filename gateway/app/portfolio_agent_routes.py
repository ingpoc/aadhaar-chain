"""Portfolio Cursor agent control plane for Buyer/Seller FQDN handoff.

Replaces FlatWatch `:43104` dependency on public hosts. Samantha short tools stay
on Realtime; long `delegate_to_runtime_agent` posts here via Vercel `/api/agent/*`
rewrites. Auth is portfolio-style `X-User-Id` (not FlatWatch session cookies).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.runtime_config import resolve_runtime_policy

router = APIRouter(tags=["portfolio-agent"])

AppId = Literal["ondc-buyer", "ondc-seller", "flatwatch"]

_CAPABILITIES: dict[str, list[str]] = {
    "ondc-buyer": [
        "search",
        "product_detail",
        "cart_state",
        "order_status",
        "trust_checkout_guidance",
    ],
    "ondc-seller": [
        "catalog_publish",
        "order_status",
        "refund_guidance",
        "trust_ops_guidance",
    ],
    "flatwatch": ["search", "order_status"],
}

_SESSIONS: dict[str, dict[str, Any]] = {}
_GATEWAY_ROOT = Path(__file__).resolve().parents[1]


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _require_subject(x_user_id: Optional[str]) -> str:
    subject = (x_user_id or "").strip()
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-User-Id is required.",
        )
    return subject


def _runtime_snapshot(app: AppId, subject_id: str) -> dict[str, Any]:
    policy = resolve_runtime_policy()
    caps = list(_CAPABILITIES.get(app, []))
    available = bool(policy.runtime_available)
    return {
        "app_id": app,
        "subject_id": subject_id,
        "auth_mode": policy.auth_mode,
        "model": policy.model,
        "runtime_available": available,
        "agent_access": available,
        "trust_state": "session",
        "trust_required_for_write": False,
        "mode": "read_write" if available else "blocked",
        "usage": {
            "requests_used": 0,
            "requests_limit": 0,
            "period_start": datetime.now(timezone.utc).isoformat(),
            "period_end": datetime.now(timezone.utc).isoformat(),
            "estimated_cost_usd": 0.0,
        },
        "allowed_capabilities": caps if available else [],
        "blocked_reason": None if available else (policy.blocked_reason or "Cursor agent runtime unavailable"),
        "compatibility_surface": "agent_runtime",
        "control_plane": "gateway",
    }


class PortfolioAgentRequest(BaseModel):
    prompt: str = Field(min_length=1)
    sessionId: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/agent/runtime")
async def portfolio_agent_runtime(
    app: AppId = Query(...),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict[str, Any]:
    subject = _require_subject(x_user_id)
    return _runtime_snapshot(app, subject)


async def _stream_cursor(
    *,
    app_id: AppId,
    subject_id: str,
    body: PortfolioAgentRequest,
):
    runtime = _runtime_snapshot(app_id, subject_id)
    if not runtime["runtime_available"] or not runtime["agent_access"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=runtime.get("blocked_reason") or "Cursor agent runtime is unavailable.",
        )

    session_key = f"{app_id}:{body.sessionId}"
    session = _SESSIONS.get(session_key)
    if session is None:
        session = {
            "session_id": body.sessionId,
            "app_id": app_id,
            "subject_id": subject_id,
            "sdk_session_id": None,
            "mode": runtime["mode"],
            "allowed_capabilities": runtime["allowed_capabilities"],
            "context": body.context,
            "messages": [],
        }
        _SESSIONS[session_key] = session

    system_prefix = (
        "You are a portfolio agent backed by the Cursor SDK on the AadhaarChain gateway.\n"
        f"App: {app_id}\n"
        f"Subject: {subject_id}\n"
        f"Mode: {session['mode']}\n"
        f"Allowed capabilities: {', '.join(session['allowed_capabilities']) or 'none'}\n"
        "Stay concise. Do not claim payment settlement or network status that the recorded tool result does not prove.\n"
        "AgentGuard authorization must complete before any protected commerce tool reports success.\n"
        f"Caller context JSON: {json.dumps(body.context)[:4000]}"
    )

    async def event_stream():
        yield f"data: {json.dumps({'type': 'init', 'session_id': body.sessionId, 'mode': session['mode']})}\n\n"

        api_key = (os.getenv("CURSOR_API_KEY") or "").strip()
        if not api_key:
            yield f"data: {json.dumps({'type': 'error', 'error': 'CURSOR_API_KEY is not configured on the gateway.', 'timestamp': _now_ms()})}\n\n"
            yield "data: [DONE]\n\n"
            return

        try:
            from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'error': f'cursor_sdk unavailable: {exc}', 'timestamp': _now_ms()})}\n\n"
            yield "data: [DONE]\n\n"
            return

        compiled = f"{system_prefix}\n\nUser request: {body.prompt}"
        model = runtime.get("model") or os.getenv("CURSOR_AGENT_MODEL") or "composer-2.5"
        resume_id = session.get("sdk_session_id")

        def _run() -> tuple[str, Optional[str]]:
            options = AgentOptions(
                api_key=api_key,
                model=model,
                local=LocalAgentOptions(cwd=str(_GATEWAY_ROOT), setting_sources=[]),
                agent_id=resume_id if isinstance(resume_id, str) else None,
            )
            if resume_id:
                with Agent.resume(resume_id, options) as agent:
                    run = agent.send(compiled)
                    result = run.wait()
                    if result.status == "error":
                        raise RuntimeError(f"Cursor agent run failed (run_id={result.id})")
                    text = (result.result or "").strip()
                    if not text:
                        raise RuntimeError("Cursor agent returned empty content.")
                    return text, agent.agent_id
            with Agent.create(options) as agent:
                run = agent.send(compiled)
                result = run.wait()
                if result.status == "error":
                    raise RuntimeError(f"Cursor agent run failed (run_id={result.id})")
                text = (result.result or "").strip()
                if not text:
                    raise RuntimeError("Cursor agent returned empty content.")
                return text, agent.agent_id

        try:
            final_text, sdk_id = await asyncio.to_thread(_run)
            session["sdk_session_id"] = sdk_id
            session["messages"].append(
                {
                    "role": "user",
                    "content": body.prompt,
                    "timestamp": _now_ms(),
                }
            )
            session["messages"].append(
                {
                    "role": "assistant",
                    "content": final_text,
                    "timestamp": _now_ms(),
                }
            )
            yield f"data: {json.dumps({'type': 'result', 'content': final_text, 'sdk_session_id': sdk_id, 'timestamp': _now_ms()})}\n\n"
        except Exception as exc:  # noqa: BLE001 — stream error to client
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc) or 'Cursor agent failed.', 'timestamp': _now_ms()})}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/api/agent/buyer")
async def portfolio_buyer_agent(
    body: PortfolioAgentRequest,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    return await _stream_cursor(
        app_id="ondc-buyer",
        subject_id=_require_subject(x_user_id),
        body=body,
    )


@router.post("/api/agent/seller")
async def portfolio_seller_agent(
    body: PortfolioAgentRequest,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    return await _stream_cursor(
        app_id="ondc-seller",
        subject_id=_require_subject(x_user_id),
        body=body,
    )
