"""Server-side ONDC / Beckn adapter scaffold (Milestone 9 / prod P3).

Signing keys stay on the gateway — never in Vite.
Frontends call /api/ondc/* ; live network traffic requires ONDC_ENABLED + keys + subscriber.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config import settings

router = APIRouter(prefix="/api/ondc", tags=["ondc"])

# In-memory outbox/inbox for local staging (replace with durable store in prod).
_OUTBOX: list[dict[str, Any]] = []
_INBOX: list[dict[str, Any]] = []


def _ondc_configured() -> bool:
    return bool(
        getattr(settings, "ondc_enabled", False)
        and getattr(settings, "ondc_subscriber_id", None)
        and getattr(settings, "ondc_bap_uri", None)
    )


def _status_payload() -> dict[str, Any]:
    key_path = getattr(settings, "ondc_signing_private_key_path", None) or ""
    has_signing = bool(key_path and Path(key_path).expanduser().is_file())
    return {
        "enabled": bool(getattr(settings, "ondc_enabled", False)),
        "configured": _ondc_configured(),
        "subscriber_id": getattr(settings, "ondc_subscriber_id", None),
        "bap_id": getattr(settings, "ondc_bap_id", None),
        "bap_uri": getattr(settings, "ondc_bap_uri", None),
        "gateway_url": getattr(settings, "ondc_gateway_url", None),
        "registry_url": getattr(settings, "ondc_registry_url", None),
        "signing_key_present": has_signing,
        "unique_key_id": getattr(settings, "ondc_unique_key_id", None),
        "outbox_depth": len(_OUTBOX),
        "inbox_depth": len(_INBOX),
        "note": (
            "Live Beckn search/confirm requires portal whitelist + TLS + keys. "
            "Until then keep VITE_COMMERCE_DEMO_MODE=true."
        ),
    }


class SearchBody(BaseModel):
    intent: dict[str, Any] = Field(default_factory=dict)
    message_id: Optional[str] = None


class ConfirmBody(BaseModel):
    order: dict[str, Any] = Field(default_factory=dict)
    message_id: Optional[str] = None


def _enqueue_outbox(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "id": f"out_{uuid.uuid4().hex[:12]}",
        "action": action,
        "payload": payload,
        "created_at": int(time.time()),
        "status": "queued",
        "idempotency_key": hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24],
    }
    _OUTBOX.append(entry)
    if len(_OUTBOX) > 200:
        del _OUTBOX[:-200]
    return entry


@router.get("/status")
async def ondc_status() -> JSONResponse:
    return JSONResponse({"success": True, "data": _status_payload()})


@router.post("/search")
async def ondc_search(body: SearchBody) -> JSONResponse:
    """Queue a Beckn search. Returns 503 until ONDC_ENABLED + config."""
    if not _ondc_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "ONDC adapter not ready. Complete Participant Portal A5–A8, "
                "set ONDC_* on gateway, keep commerce demo mode until then."
            ),
        )
    message_id = body.message_id or f"msg_{uuid.uuid4().hex[:12]}"
    envelope = {
        "context": {
            "action": "search",
            "message_id": message_id,
            "bap_id": settings.ondc_bap_id or settings.ondc_subscriber_id,
            "bap_uri": settings.ondc_bap_uri,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        },
        "message": {"intent": body.intent},
    }
    entry = _enqueue_outbox("search", envelope)
    return JSONResponse(
        {
            "success": True,
            "data": {
                "queued": True,
                "outbox_id": entry["id"],
                "message_id": message_id,
                "ack": "ACK",
                "note": "Staging dispatch to gateway/registry not yet wired — envelope queued.",
            },
        }
    )


@router.post("/confirm")
async def ondc_confirm(body: ConfirmBody) -> JSONResponse:
    if not _ondc_configured():
        raise HTTPException(status_code=503, detail="ONDC adapter not ready.")
    message_id = body.message_id or f"msg_{uuid.uuid4().hex[:12]}"
    envelope = {
        "context": {
            "action": "confirm",
            "message_id": message_id,
            "bap_id": settings.ondc_bap_id or settings.ondc_subscriber_id,
            "bap_uri": settings.ondc_bap_uri,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        },
        "message": {"order": body.order},
    }
    entry = _enqueue_outbox("confirm", envelope)
    return JSONResponse(
        {
            "success": True,
            "data": {
                "queued": True,
                "outbox_id": entry["id"],
                "message_id": message_id,
                "ack": "ACK",
            },
        }
    )


@router.post("/callback/{action}")
async def ondc_callback(action: str, body: dict[str, Any]) -> JSONResponse:
    """Beckn on_search / on_confirm style callbacks land here."""
    entry = {
        "id": f"in_{uuid.uuid4().hex[:12]}",
        "action": action,
        "payload": body,
        "received_at": int(time.time()),
    }
    _INBOX.append(entry)
    if len(_INBOX) > 200:
        del _INBOX[:-200]
    return JSONResponse({"message": {"ack": {"status": "ACK"}}})


@router.get("/outbox")
async def ondc_outbox() -> JSONResponse:
    return JSONResponse({"success": True, "data": {"items": list(reversed(_OUTBOX[-50:]))}})


@router.get("/inbox")
async def ondc_inbox() -> JSONResponse:
    return JSONResponse({"success": True, "data": {"items": list(reversed(_INBOX[-50:]))}})
