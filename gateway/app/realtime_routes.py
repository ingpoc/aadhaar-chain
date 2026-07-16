"""OpenAI Realtime ephemeral client secrets for Samantha (Buyer / Seller voice)."""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from config import settings
from app.samantha_transcripts import append_event, list_events
from app.session_auth import SESSION_COOKIE_NAME, parse_session_token

router = APIRouter(prefix="/api/realtime", tags=["realtime"])

# Tools are also registered client-side via session.update; minting here primes the session.
BUYER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_catalog",
        "description": "Short tool: search the ONDC catalog. Chain with add_to_cart when needed.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "navigate_to",
        "description": "Short tool: navigate Buyer UI to an allowlisted path (/search, /cart, /checkout, /orders, /config, /agent).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "add_to_cart",
        "description": "Short tool: add one item by exact search_catalog id.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "quantity": {"type": "number"},
            },
            "required": ["item_id"],
        },
    },
    {
        "type": "function",
        "name": "clear_cart",
        "description": "Empty the live Buyer cart and open /cart.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "remove_from_cart",
        "description": "Remove one live Buyer cart line by item id or product query.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "set_cart_quantity",
        "description": "Set the final quantity of one live Buyer cart line. Zero removes it.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "query": {"type": "string"},
                "quantity": {"type": "number"},
            },
            "required": ["quantity"],
        },
    },
    {
        "type": "function",
        "name": "checkout_commit",
        "description": "Short guarded tool: AgentGuard checkout commit.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount_inr": {"type": "number"},
                "session_id": {"type": "string"},
            },
            "required": ["amount_inr", "session_id"],
        },
    },
    {
        "type": "function",
        "name": "remember_preference",
        "description": "Short tool: store a compact preference.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["like", "dislike", "preference", "note"]},
                "value": {"type": "string"},
            },
            "required": ["kind", "value"],
        },
    },
    {
        "type": "function",
        "name": "delegate_to_runtime_agent",
        "description": "Start long/multi-step planning in the background as Samantha. Never send user to /agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "context": {"type": "object", "additionalProperties": True},
            },
            "required": ["task"],
        },
    },
]

SELLER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "navigate_to",
        "description": "Short tool: navigate Seller UI to an allowlisted path (/catalog, /orders, /agentguard, /config, /dashboard, /agent).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "catalog_publish",
        "description": "Short tool: publish one catalog item.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "price_inr": {"type": "number"},
                "inventory": {"type": "number"},
                "description": {"type": "string"},
            },
            "required": ["title", "price_inr"],
        },
    },
    {
        "type": "function",
        "name": "refund_issue",
        "description": "Short guarded tool: one AgentGuard refund.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount_inr": {"type": "number"},
            },
            "required": ["order_id", "amount_inr"],
        },
    },
    {
        "type": "function",
        "name": "accept_order",
        "description": "Accept one paid Seller order through AgentGuard. Omit order_id for the newest eligible order.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "reject_order",
        "description": "Reject one paid Seller order through AgentGuard. Omit order_id for the newest eligible order.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "mark_order_fulfilled",
        "description": "Mark one accepted Seller order fulfilled through AgentGuard. Omit order_id for the newest eligible order.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "remember_preference",
        "description": "Short tool: store a compact seller ops preference.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["like", "dislike", "preference", "note"]},
                "value": {"type": "string"},
            },
            "required": ["kind", "value"],
        },
    },
    {
        "type": "function",
        "name": "delegate_to_runtime_agent",
        "description": "Start long/multi-step seller ops in the background as Samantha. Never send user to /agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "context": {"type": "object", "additionalProperties": True},
            },
            "required": ["task"],
        },
    },
]

SAMANTHA_BUYER = (
    "You are Samantha, the ONDC Buyer shopping companion. Speak briefly and warmly. "
    "Interpret the user's intent, then act with tools — never narrate a search, cart add, or navigation without calling the tool. "
    "Greetings or chitchat: reply briefly with no tools. Do not volunteer work they did not ask for. "
    "Actionable short asks (find/search/add/change cart/checkout/open cart): you MUST call the right tool(s) immediately. "
    "For add-to-cart product asks: call search_catalog then add_to_cart with an exact item_id from search results. "
    "Chain several short tools in one turn when one request needs multiple steps. "
    "Never claim an action without a successful tool call. "
    "Long or multi-step planning: call delegate_to_runtime_agent once. "
    "When that tool returns started: say you started and will let them know when done — never mention another agent, Cursor, or /agent. "
    "Never claim longer work finished unless a later update says so. "
    "Never invent work the user did not ask for. Report AgentGuard outcomes honestly. Do not send users to /agent. "
    "Never say cart actions are unavailable: use clear_cart, remove_from_cart, or set_cart_quantity. "
    "Short tools: search_catalog, navigate_to, add_to_cart, clear_cart, remove_from_cart, set_cart_quantity, remember_preference, checkout_commit. "
    "Use stored user memory when suggesting products."
)

SAMANTHA_SELLER = (
    "You are Samantha, the ONDC Seller operations companion. Speak briefly. "
    "Interpret the user's intent, then act. "
    "Greetings or chitchat: reply briefly with no tools. Do not volunteer work they did not ask for. "
    "Actionable short asks: choose and call the right tool(s). Chain several short tools in one turn when one request needs multiple steps. "
    "Never claim an action without a successful tool call. "
    "Long or multi-step ops: call delegate_to_runtime_agent once. "
    "When that tool returns started: say you started and will let them know when done — never mention another agent, Cursor, or /agent. "
    "Never claim longer work finished unless a later update says so. "
    "Never invent work the user did not ask for. Report AgentGuard allow / need_approval / deny honestly. Do not send users to /agent. "
    "Short tools: navigate_to, catalog_publish, refund_issue, remember_preference."
)


class RealtimeSessionRequest(BaseModel):
    role: str = Field(default="buyer")
    instructions: Optional[str] = None
    memory_prompt: Optional[str] = None
    agent_name: str = Field(default="Samantha")


class TranscriptEventRequest(BaseModel):
    role: str = Field(pattern="^(buyer|seller)$")
    session_id: str = Field(min_length=8, max_length=160)
    event_type: str = Field(min_length=3, max_length=64)
    content: str = Field(default="", max_length=4_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _authenticated_session(request: Request, role: str | None = None) -> dict[str, Any]:
    session = parse_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))
    if session is None:
        raise HTTPException(status_code=401, detail="Sign in before using Samantha.")
    if role and session.get("aud") not in {role, f"ondc{role}"}:
        raise HTTPException(status_code=403, detail="Samantha session does not match this app.")
    return session


def _openai_api_key() -> str:
    key = (getattr(settings, "openai_api_key", None) or os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY not configured on gateway — Realtime voice unavailable.",
        )
    return key


def _realtime_model() -> str:
    return (
        getattr(settings, "openai_realtime_model", None)
        or os.getenv("OPENAI_REALTIME_MODEL")
        or "gpt-realtime-2.1-mini"
    ).strip()


def _role_instructions(role: str) -> str:
    return SAMANTHA_SELLER if (role or "").strip().lower() == "seller" else SAMANTHA_BUYER


def _role_tools(role: str) -> list[dict[str, Any]]:
    return SELLER_TOOLS if (role or "").strip().lower() == "seller" else BUYER_TOOLS


@router.post("/client-secret")
async def create_realtime_client_secret(body: RealtimeSessionRequest, request: Request) -> dict[str, Any]:
    """Mint an ephemeral Realtime client secret for browser WebRTC (never expose long-lived keys).

    Tools + tool_choice are registered on the session at mint time (role-based).
    Clients may still session.update the same tools on data-channel open.
    """
    api_key = _openai_api_key()
    model = _realtime_model()
    role = (body.role or "buyer").strip().lower()
    _authenticated_session(request, role)
    memory = (body.memory_prompt or "No stored preferences yet.").strip()
    instructions = body.instructions or (
        f"{_role_instructions(role)}\n\nUser memory:\n{memory}"
    )
    payload = {
        "session": {
            "type": "realtime",
            "model": model,
            "instructions": instructions,
            "tools": _role_tools(role),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {"effort": "low"},
        }
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI Realtime client_secrets failed: {response.status_code} {response.text[:400]}",
        )
    data = response.json()
    return {
        "success": True,
        "data": {
            "client_secret": data.get("value") or data.get("client_secret") or data,
            "model": model,
            "agent_name": body.agent_name or "Samantha",
            "role": role,
            "tools_registered": [t["name"] for t in _role_tools(role)],
            "expires_at": data.get("expires_at"),
        },
    }


@router.post("/transcripts/events")
async def persist_transcript_event(body: TranscriptEventRequest, request: Request) -> dict[str, Any]:
    session = _authenticated_session(request, body.role)
    try:
        event = append_event(
            principal_id=str(session["principal_id"]), role=body.role,
            session_id=body.session_id, event_type=body.event_type,
            content=body.content, metadata=body.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"success": True, "data": {"event": event}}


@router.get("/transcripts")
async def get_transcript_events(
    request: Request,
    role: str | None = Query(default=None, pattern="^(buyer|seller)$"),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    session = _authenticated_session(request, role)
    events = list_events(principal_id=str(session["principal_id"]), role=role, limit=limit)
    return {"success": True, "data": {"events": events, "count": len(events)}}


@router.get("/status")
async def realtime_status() -> dict[str, Any]:
    configured = bool(
        (getattr(settings, "openai_api_key", None) or os.getenv("OPENAI_API_KEY") or "").strip()
    )
    return {
        "success": True,
        "data": {
            "configured": configured,
            "model": _realtime_model() if configured else None,
            "agent_name": "Samantha",
        },
    }
