"""Server-side ONDC / Beckn BAP adapter (Milestone 9 / P3).

Signing keys stay on the gateway — never in Vite.
Frontends call /api/ondc/* ; PreProd traffic requires ONDC_ENABLED + keys + subscriber.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import ondc_store
from app.ondc_crypto import create_authorization_header, load_ed25519_private_pem, minify_json
from config import settings

router = APIRouter(tags=["ondc"])

PREPROD_GATEWAY = "https://preprod.gateway.ondc.org/search"
PREPROD_LOOKUP = "https://preprod.registry.ondc.org/v2.0/lookup"
DEFAULT_CITY = "std:080"
DEFAULT_DOMAIN = "ONDC:RET10"
CORE_VERSION = "1.2.0"


def _buyer_paths() -> dict[str, Path]:
    from app.ondc_onboard_routes import _role_paths

    return _role_paths("buyer")


def _buyer_uk_id() -> Optional[str]:
    if getattr(settings, "ondc_unique_key_id", None):
        return str(settings.ondc_unique_key_id).strip() or None
    paths = _buyer_paths()
    if paths["uk_id"].is_file():
        value = paths["uk_id"].read_text(encoding="utf-8").strip()
        if value:
            return value
    if paths["meta"].is_file():
        try:
            meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
            uk = meta.get("unique_key_id")
            if uk:
                return str(uk).strip()
        except json.JSONDecodeError:
            pass
    return None


def _subscriber_id() -> Optional[str]:
    return (
        getattr(settings, "ondc_subscriber_id", None)
        or getattr(settings, "ondc_bap_id", None)
        or getattr(settings, "ondc_buyer_subscriber_id", None)
    )


def _bap_uri() -> Optional[str]:
    configured = getattr(settings, "ondc_bap_uri", None)
    if configured:
        return configured.rstrip("/")
    sid = _subscriber_id()
    if sid and "." in sid:
        return f"https://{sid}/ondc"
    return None


def _gateway_url() -> str:
    return (getattr(settings, "ondc_gateway_url", None) or PREPROD_GATEWAY).strip()


def _registry_url() -> str:
    return (getattr(settings, "ondc_registry_url", None) or PREPROD_LOOKUP).strip()


def _signing_pem_path() -> Optional[Path]:
    configured = getattr(settings, "ondc_signing_private_key_path", None)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path
    paths = _buyer_paths()
    if paths["signing_pem"].is_file():
        return paths["signing_pem"]
    return None


def _load_buyer_signing_key():
    path = _signing_pem_path()
    if path is None:
        raise HTTPException(status_code=503, detail="ONDC Buyer signing key missing")
    return load_ed25519_private_pem(path.read_bytes())


def _ondc_configured() -> bool:
    return bool(
        getattr(settings, "ondc_enabled", False)
        and _subscriber_id()
        and _bap_uri()
        and _buyer_uk_id()
        and _signing_pem_path() is not None
    )


def _status_payload() -> dict[str, Any]:
    pem = _signing_pem_path()
    return {
        "enabled": bool(getattr(settings, "ondc_enabled", False)),
        "configured": _ondc_configured(),
        "subscriber_id": _subscriber_id(),
        "bap_id": getattr(settings, "ondc_bap_id", None) or _subscriber_id(),
        "bap_uri": _bap_uri(),
        "gateway_url": _gateway_url(),
        "registry_url": _registry_url(),
        "signing_key_present": pem is not None,
        "unique_key_id": _buyer_uk_id(),
        "registry_env": getattr(settings, "ondc_registry_env", "preprod"),
        "outbox_depth": len(ondc_store.list_outbox(limit=500)),
        "inbox_depth": len(ondc_store.list_inbox(limit=500)),
        "note": (
            "PreProd: signed lookup + search dispatch when enabled+configured. "
            "Do not flip VITE_COMMERCE_DEMO_MODE without commerce_demo_mode_gate evidence."
        ),
    }


class SearchBody(BaseModel):
    intent: dict[str, Any] = Field(default_factory=dict)
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None
    city: Optional[str] = None
    domain: Optional[str] = None
    query: Optional[str] = None


class ConfirmBody(BaseModel):
    order: dict[str, Any] = Field(default_factory=dict)
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None


class LookupBody(BaseModel):
    subscriber_id: Optional[str] = None
    domain: Optional[str] = None
    ukId: Optional[str] = None
    type: Optional[str] = None
    country: Optional[str] = "IND"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def _build_search_envelope(body: SearchBody) -> dict[str, Any]:
    message_id = body.message_id or str(uuid.uuid4())
    transaction_id = body.transaction_id or str(uuid.uuid4())
    intent = dict(body.intent or {})
    if body.query and not (intent.get("item") or {}).get("descriptor"):
        intent.setdefault("item", {})
        intent["item"].setdefault("descriptor", {})
        intent["item"]["descriptor"]["name"] = body.query
    if "fulfillment" not in intent:
        intent["fulfillment"] = {
            "type": "Delivery",
            "end": {
                "location": {
                    "gps": "12.9715987,77.5945627",
                    "address": {"area_code": "560001"},
                }
            },
        }
    bap_id = getattr(settings, "ondc_bap_id", None) or _subscriber_id()
    return {
        "context": {
            "domain": body.domain or DEFAULT_DOMAIN,
            "action": "search",
            "country": "IND",
            "city": body.city or DEFAULT_CITY,
            "core_version": CORE_VERSION,
            "bap_id": bap_id,
            "bap_uri": _bap_uri(),
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": _iso_now(),
            "ttl": "PT30S",
        },
        "message": {"intent": intent},
    }


async def _signed_post(url: str, payload: dict[str, Any]) -> tuple[int, Any, str]:
    subscriber_id = _subscriber_id()
    uk_id = _buyer_uk_id()
    if not subscriber_id or not uk_id:
        raise HTTPException(status_code=503, detail="ONDC subscriber_id / unique_key_id missing")
    private_key = _load_buyer_signing_key()
    body_str = minify_json(payload)
    auth = create_authorization_header(
        body_str,
        subscriber_id=subscriber_id,
        unique_key_id=uk_id,
        private_key=private_key,
    )
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(
            url,
            content=body_str.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": auth,
            },
        )
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {"raw": resp.text[:2000]}
    return resp.status_code, data, body_str


@router.get("/api/ondc/status")
async def ondc_status() -> JSONResponse:
    return JSONResponse({"success": True, "data": _status_payload()})


@router.post("/api/ondc/lookup")
async def ondc_lookup(body: LookupBody) -> JSONResponse:
    """Signed PreProd/staging registry lookup."""
    if not getattr(settings, "ondc_enabled", False):
        raise HTTPException(status_code=503, detail="ONDC_ENABLED=false")
    if _signing_pem_path() is None or not _buyer_uk_id():
        raise HTTPException(status_code=503, detail="ONDC Buyer keys / uk_id not ready")
    payload: dict[str, Any] = {
        "subscriber_id": body.subscriber_id or _subscriber_id(),
        "domain": body.domain or DEFAULT_DOMAIN,
        "country": body.country or "IND",
    }
    if body.type:
        payload["type"] = body.type
    if body.ukId or _buyer_uk_id():
        payload["ukId"] = body.ukId or _buyer_uk_id()
    status, data, _ = await _signed_post(_registry_url(), payload)
    return JSONResponse(
        {
            "success": status < 400,
            "data": {
                "http_status": status,
                "registry_url": _registry_url(),
                "request": payload,
                "response": data,
            },
        },
        status_code=200 if status < 500 else 502,
    )


@router.post("/api/ondc/search")
async def ondc_search(body: SearchBody) -> JSONResponse:
    """Signed Beckn search → PreProd gateway; persist outbox status."""
    if not _ondc_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "ONDC adapter not ready. Set ONDC_ENABLED=true, subscriber/bap_uri, "
                "Buyer signing PEM + unique_key_id."
            ),
        )
    envelope = _build_search_envelope(body)
    message_id = envelope["context"]["message_id"]
    transaction_id = envelope["context"]["transaction_id"]
    entry = {
        "id": f"out_{uuid.uuid4().hex[:12]}",
        "action": "search",
        "payload": envelope,
        "created_at": int(time.time()),
        "status": "queued",
        "message_id": message_id,
        "transaction_id": transaction_id,
        "idempotency_key": hashlib.sha256(
            json.dumps(envelope, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24],
    }
    ondc_store.append_outbox(entry)
    try:
        status, data, _ = await _signed_post(_gateway_url(), envelope)
    except HTTPException:
        ondc_store.update_outbox(entry["id"], status="error", error="signing/config")
        raise
    except Exception as exc:  # noqa: BLE001
        ondc_store.update_outbox(entry["id"], status="error", error=str(exc))
        raise HTTPException(status_code=502, detail=f"ONDC gateway dispatch failed: {exc}") from exc

    ack = None
    if isinstance(data, dict):
        ack = ((data.get("message") or {}).get("ack") or {}).get("status")
    dispatch_status = "sent" if status < 400 else "nack"
    if ack == "NACK":
        dispatch_status = "nack"
    ondc_store.update_outbox(
        entry["id"],
        status=dispatch_status,
        http_status=status,
        gateway_response=data,
    )
    return JSONResponse(
        {
            "success": dispatch_status == "sent",
            "data": {
                "queued": False,
                "dispatched": True,
                "outbox_id": entry["id"],
                "message_id": message_id,
                "transaction_id": transaction_id,
                "http_status": status,
                "ack": ack,
                "gateway_url": _gateway_url(),
                "gateway_response": data,
                "note": "Poll GET /api/ondc/catalogs?transaction_id=… for on_search results.",
            },
        }
    )


@router.post("/api/ondc/confirm")
async def ondc_confirm(body: ConfirmBody) -> JSONResponse:
    if not _ondc_configured():
        raise HTTPException(status_code=503, detail="ONDC adapter not ready.")
    message_id = body.message_id or str(uuid.uuid4())
    transaction_id = body.transaction_id or str(uuid.uuid4())
    bap_id = getattr(settings, "ondc_bap_id", None) or _subscriber_id()
    envelope = {
        "context": {
            "domain": DEFAULT_DOMAIN,
            "action": "confirm",
            "country": "IND",
            "city": DEFAULT_CITY,
            "core_version": CORE_VERSION,
            "bap_id": bap_id,
            "bap_uri": _bap_uri(),
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": _iso_now(),
            "ttl": "PT30S",
        },
        "message": {"order": body.order},
    }
    entry = {
        "id": f"out_{uuid.uuid4().hex[:12]}",
        "action": "confirm",
        "payload": envelope,
        "created_at": int(time.time()),
        "status": "queued",
        "message_id": message_id,
        "transaction_id": transaction_id,
    }
    ondc_store.append_outbox(entry)
    # Confirm goes to bpp_uri normally; keep as outbox until select/init wired.
    ondc_store.update_outbox(entry["id"], status="held", note="confirm requires bpp_uri from on_select")
    return JSONResponse(
        {
            "success": True,
            "data": {
                "queued": True,
                "outbox_id": entry["id"],
                "message_id": message_id,
                "transaction_id": transaction_id,
                "ack": "ACK",
                "note": "confirm dispatch to BPP not yet wired — use search e2e first.",
            },
        }
    )


async def _ingest_callback(action: str, body: dict[str, Any]) -> JSONResponse:
    ctx = body.get("context") or {}
    entry = {
        "id": f"in_{uuid.uuid4().hex[:12]}",
        "action": action if action.startswith("on_") else f"on_{action}",
        "payload": body,
        "received_at": int(time.time()),
        "transaction_id": ctx.get("transaction_id"),
        "message_id": ctx.get("message_id"),
        "bpp_id": ctx.get("bpp_id"),
    }
    ondc_store.append_inbox(entry)
    return JSONResponse({"message": {"ack": {"status": "ACK"}}})


@router.post("/api/ondc/callback/{action}")
async def ondc_callback_api(action: str, body: dict[str, Any]) -> JSONResponse:
    return await _ingest_callback(action, body)


_BECKN_CALLBACK_ACTIONS = (
    "search",
    "select",
    "init",
    "confirm",
    "status",
    "track",
    "cancel",
    "update",
    "rating",
    "support",
)


def _register_beckn_callbacks() -> None:
    """Explicit paths so /ondc/on_subscribe stays on onboard router."""

    for act in _BECKN_CALLBACK_ACTIONS:

        async def _root(request: Request, action: str = act) -> JSONResponse:
            body = await request.json()
            return await _ingest_callback(f"on_{action}", body)

        async def _np(role: str, request: Request, action: str = act) -> JSONResponse:
            if role not in {"buyer", "seller"}:
                raise HTTPException(status_code=404, detail="role must be buyer|seller")
            body = await request.json()
            return await _ingest_callback(f"on_{action}", body)

        router.add_api_route(
            f"/ondc/on_{act}",
            _root,
            methods=["POST"],
            name=f"ondc_on_{act}",
        )
        router.add_api_route(
            f"/ondc/np/{{role}}/on_{act}",
            _np,
            methods=["POST"],
            name=f"ondc_np_on_{act}",
        )


_register_beckn_callbacks()


@router.get("/api/ondc/outbox")
async def ondc_outbox() -> JSONResponse:
    return JSONResponse({"success": True, "data": {"items": ondc_store.list_outbox()}})


@router.get("/api/ondc/inbox")
async def ondc_inbox(action: Optional[str] = None) -> JSONResponse:
    return JSONResponse(
        {"success": True, "data": {"items": ondc_store.list_inbox(action=action)}}
    )


@router.get("/api/ondc/catalogs")
async def ondc_catalogs(transaction_id: str) -> JSONResponse:
    if not transaction_id.strip():
        raise HTTPException(status_code=400, detail="transaction_id required")
    items = ondc_store.catalogs_for_transaction(transaction_id.strip())
    return JSONResponse(
        {
            "success": True,
            "data": {
                "transaction_id": transaction_id,
                "items": items,
                "count": len(items),
                "source": "ondc-network",
            },
        }
    )
