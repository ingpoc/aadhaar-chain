"""Server-side ONDC / Beckn BAP adapter (Milestone 9 / P3).

Signing keys stay on the gateway — never in Vite.
Frontends call /api/ondc/* ; PreProd traffic requires ONDC_ENABLED + keys + subscriber.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import ondc_store
from app.ondc_crypto import create_authorization_header, load_ed25519_private_pem, minify_json
from app.persistence.ondc_repository import (
    CorrelationMismatch,
    EnvelopeCommitmentMismatch,
    ONDCRepository,
    persist_callback_before_ack,
)
from app.persistence.transaction import UnitOfWork
from config import settings

router = APIRouter(tags=["ondc"])

PREPROD_GATEWAY = "https://preprod.gateway.ondc.org/search"
PREPROD_LOOKUP = "https://preprod.registry.ondc.org/v2.0/lookup"
DEFAULT_CITY = "std:080"
DEFAULT_DOMAIN = "ONDC:RET10"
CORE_VERSION = "1.2.0"


async def _stage_outbox_before_dispatch(
    request: Request,
    entry: dict[str, Any],
    *,
    destination: str,
) -> dict[str, Any]:
    """Persist a delivery intent, then claim it before any network effect."""
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        ondc_store.append_outbox(entry)
        return {
            "backend": "local_file",
            "public_id": entry["id"],
            "entry": entry,
        }

    context = entry["payload"]["context"]
    correlation_id = (
        request.headers.get("X-Correlation-ID")
        or str(context["transaction_id"])
    ).strip()
    async with UnitOfWork(pool) as unit_of_work:
        repository = ONDCRepository(unit_of_work)
        prior_records = await repository.list_for_transaction(
            "outbox",
            str(context["transaction_id"]),
            action=str(entry["action"]),
            limit=100,
        )
        for prior in prior_records:
            if (
                prior["message_id"] != str(context["message_id"])
                or prior["destination"] != destination
            ):
                continue
            prior_envelope = prior.get("envelope") or {}
            candidate_envelope = entry["payload"]
            prior_comparable = json.loads(json.dumps(prior_envelope))
            candidate_comparable = json.loads(json.dumps(candidate_envelope))
            (prior_comparable.get("context") or {}).pop("timestamp", None)
            (candidate_comparable.get("context") or {}).pop("timestamp", None)
            if prior_comparable == candidate_comparable:
                entry["payload"] = prior_envelope
                context = prior_envelope["context"]
            break
        created, persisted = await repository.enqueue_outbox(
            subscriber_id=str(context.get("bap_id") or _subscriber_id() or ""),
            transaction_id=str(context["transaction_id"]),
            message_id=str(context["message_id"]),
            action=str(entry["action"]),
            destination=destination,
            raw_envelope=entry["payload"],
            redacted_payload={"status": "queued"},
            correlation_id=correlation_id,
        )
        if persisted["state"] == "delivered":
            return {
                "backend": "postgres",
                "public_id": f"pg_out_{persisted['outbox_id']}",
                "persisted": persisted,
                "created": created,
                "already_delivered": True,
                "pool": pool,
            }
        claimed = await repository.claim_outbox_record(
            persisted["outbox_id"],
            worker_id=f"inline:{uuid.uuid4().hex}",
            lease_seconds=30,
        )
    if claimed is None:
        raise HTTPException(
            status_code=409,
            detail="ONDC delivery is already leased; retry with the same message_id",
        )
    return {
        "backend": "postgres",
        "public_id": f"pg_out_{claimed['outbox_id']}",
        "persisted": claimed,
        "created": created,
        "already_delivered": False,
        "pool": pool,
    }


async def _complete_outbox_delivery(
    staged: dict[str, Any],
    *,
    delivered: bool,
    error: str | None = None,
    file_updates: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Finish the active lease or make the durable delivery retryable."""
    if staged["backend"] == "local_file":
        ondc_store.update_outbox(
            staged["entry"]["id"], **(file_updates or {})
        )
        return None

    claimed = staged["persisted"]
    async with UnitOfWork(staged["pool"]) as unit_of_work:
        repository = ONDCRepository(unit_of_work)
        if delivered:
            return await repository.mark_delivered(
                "outbox", claimed["outbox_id"], claimed["lease_token"]
            )
        return await repository.schedule_retry(
            "outbox",
            claimed["outbox_id"],
            claimed["lease_token"],
            error=(error or "ONDC delivery failed")[:2000],
        )


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
            "PreProd: signed lookup + search + select/init/confirm when enabled+configured. "
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
    include_configured_bpp: bool = False


class DeadLetterRecoveryBody(BaseModel):
    event_commitment: str = Field(min_length=64, max_length=64)


class OutboxDrainBody(BaseModel):
    worker_id: str = Field(default="ondc-recovery", min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=100)
    lease_seconds: int = Field(default=30, ge=1, le=300)


class OrderActionBody(BaseModel):
    """select / init / confirm — order + target BPP."""

    order: dict[str, Any] = Field(default_factory=dict)
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[str] = None
    city: Optional[str] = None
    domain: Optional[str] = None


class ConfirmBody(OrderActionBody):
    """Backward-compatible alias for confirm."""


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
    payment = intent.setdefault("payment", {})
    payment.setdefault("@ondc/org/buyer_app_finder_fee_type", "Percent")
    payment.setdefault("@ondc/org/buyer_app_finder_fee_amount", "0")
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
async def ondc_status(request: Request) -> JSONResponse:
    data = _status_payload()
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is not None:
        async with UnitOfWork(pool) as unit_of_work:
            repository = ONDCRepository(unit_of_work)
            data["outbox_depth"] = len(
                await repository.list_records("outbox", limit=500)
            )
            data["inbox_depth"] = len(
                await repository.list_records("inbox", limit=500)
            )
        data["persistence_backend"] = "postgres"
    else:
        data["persistence_backend"] = "local_file"
    return JSONResponse({"success": True, "data": data})


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
async def ondc_search(body: SearchBody, request: Request) -> JSONResponse:
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
    gateway_url = _gateway_url()
    staged = await _stage_outbox_before_dispatch(
        request, entry, destination=gateway_url
    )
    if staged.get("already_delivered"):
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "queued": False,
                    "dispatched": False,
                    "deduplicated": True,
                    "outbox_id": staged["public_id"],
                    "message_id": message_id,
                    "transaction_id": transaction_id,
                    "gateway_url": gateway_url,
                },
            }
        )
    try:
        status, data, _ = await _signed_post(gateway_url, envelope)
    except HTTPException:
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error="signing/config",
            file_updates={"status": "error", "error": "signing/config"},
        )
        raise
    except Exception as exc:  # noqa: BLE001
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error=str(exc),
            file_updates={"status": "error", "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=f"ONDC gateway dispatch failed: {exc}") from exc

    ack = None
    if isinstance(data, dict):
        ack = ((data.get("message") or {}).get("ack") or {}).get("status")
    dispatch_status = "sent" if status < 400 else "nack"
    if ack == "NACK":
        dispatch_status = "nack"
    await _complete_outbox_delivery(
        staged,
        delivered=dispatch_status == "sent",
        error=f"gateway returned HTTP {status} ack={ack}",
        file_updates={
            "status": dispatch_status,
            "http_status": status,
            "gateway_response": data,
        },
    )
    direct_bpp: Optional[dict[str, Any]] = None
    if body.include_configured_bpp:
        # Portfolio proof: keep normal PreProd fanout, and send the same signed
        # Beckn search to the server-configured BPP. Callers cannot supply a URL.
        # An explicitly blank Render env used to suppress the Settings default,
        # leaving direct_bpp=null while gateway fanout still ACKed. Keep the
        # portfolio Seller route fail-closed and deterministic.
        bpp_uri = str(
            getattr(settings, "ondc_bpp_uri", None)
            or "https://ondcseller.aadharcha.in/ondc"
        ).rstrip("/")
        if bpp_uri:
            direct_target = f"{bpp_uri}/search"
            direct_staged = staged
            if staged["backend"] == "postgres":
                direct_entry = {
                    **entry,
                    "id": f"out_{uuid.uuid4().hex[:12]}",
                }
                direct_staged = await _stage_outbox_before_dispatch(
                    request, direct_entry, destination=direct_target
                )
            try:
                if direct_staged.get("already_delivered"):
                    direct_bpp = {
                        "bpp_uri": bpp_uri,
                        "ok": True,
                        "deduplicated": True,
                        "outbox_id": direct_staged["public_id"],
                    }
                    bpp_status = 200
                    bpp_data = {}
                else:
                    bpp_status, bpp_data, _ = await _signed_post(
                        direct_target, envelope
                    )
                bpp_ack = None
                if isinstance(bpp_data, dict):
                    bpp_ack = ((bpp_data.get("message") or {}).get("ack") or {}).get("status")
                bpp_delivered = bpp_status < 400 and bpp_ack != "NACK"
                if not direct_staged.get("already_delivered"):
                    await _complete_outbox_delivery(
                        direct_staged,
                        delivered=bpp_delivered,
                        error=f"configured BPP returned HTTP {bpp_status} ack={bpp_ack}",
                        file_updates={},
                    )
                    direct_bpp = {
                        "bpp_uri": bpp_uri,
                        "http_status": bpp_status,
                        "ack": bpp_ack,
                        "ok": bpp_delivered,
                    }
                    if direct_staged["backend"] == "postgres":
                        direct_bpp["outbox_id"] = direct_staged["public_id"]
                if staged["backend"] == "local_file":
                    ondc_store.update_outbox(
                        entry["id"],
                        direct_bpp_response=bpp_data,
                        direct_bpp_status=bpp_status,
                    )
            except Exception as exc:  # noqa: BLE001
                if (
                    direct_staged["backend"] == "postgres"
                    and not direct_staged.get("already_delivered")
                ):
                    await _complete_outbox_delivery(
                        direct_staged,
                        delivered=False,
                        error=str(exc),
                    )
                direct_bpp = {"bpp_uri": bpp_uri, "ok": False, "error": str(exc)}
    return JSONResponse(
        {
            "success": dispatch_status == "sent",
            "data": {
                "queued": False,
                "dispatched": True,
                "outbox_id": staged["public_id"],
                "message_id": message_id,
                "transaction_id": transaction_id,
                "http_status": status,
                "ack": ack,
                "gateway_url": gateway_url,
                "gateway_response": data,
                "direct_bpp": direct_bpp,
                "note": "Poll GET /api/ondc/catalogs?transaction_id=… for on_search results.",
            },
        }
    )


async def _resolve_bpp_target(
    request: Request, body: OrderActionBody, *, transaction_id: str
) -> tuple[str, str]:
    """Resolve bpp_id + bpp_uri from body or prior on_search catalogs."""
    bpp_id = (body.bpp_id or "").strip()
    bpp_uri = (body.bpp_uri or "").rstrip("/")
    if bpp_id and bpp_uri:
        return bpp_id, bpp_uri
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        catalogs = ondc_store.catalogs_for_transaction(transaction_id)
    else:
        async with UnitOfWork(pool) as unit_of_work:
            durable_callbacks = await ONDCRepository(
                unit_of_work
            ).list_for_transaction(
                "inbox", transaction_id, action="on_search"
            )
        catalogs = [
            {
                "bpp_id": row["envelope"].get("context", {}).get("bpp_id"),
                "bpp_uri": row["envelope"].get("context", {}).get("bpp_uri"),
            }
            for row in durable_callbacks
        ]
    for row in catalogs:
        if not bpp_id and row.get("bpp_id"):
            bpp_id = str(row["bpp_id"])
        if not bpp_uri and row.get("bpp_uri"):
            bpp_uri = str(row["bpp_uri"]).rstrip("/")
        if bpp_id and bpp_uri:
            break
    if not bpp_id:
        bpp_id = getattr(settings, "ondc_bpp_id", None) or "ondcseller.aadharcha.in"
    if not bpp_uri:
        bpp_uri = (
            getattr(settings, "ondc_bpp_uri", None)
            or f"https://{bpp_id}/ondc"
        ).rstrip("/")
    return bpp_id, bpp_uri


async def _dispatch_order_action(
    request: Request, action: str, body: OrderActionBody
) -> JSONResponse:
    """Signed select/init/confirm → bpp_uri/{action}; persist outbox."""
    if action not in {"select", "init", "confirm"}:
        raise HTTPException(status_code=400, detail=f"unsupported action: {action}")
    if not _ondc_configured():
        raise HTTPException(status_code=503, detail="ONDC adapter not ready.")
    message_id = body.message_id or str(uuid.uuid4())
    transaction_id = body.transaction_id or str(uuid.uuid4())
    bpp_id, bpp_uri = await _resolve_bpp_target(
        request, body, transaction_id=transaction_id
    )
    bap_id = getattr(settings, "ondc_bap_id", None) or _subscriber_id()
    envelope = {
        "context": {
            "domain": body.domain or DEFAULT_DOMAIN,
            "action": action,
            "country": "IND",
            "city": body.city or DEFAULT_CITY,
            "core_version": CORE_VERSION,
            "bap_id": bap_id,
            "bap_uri": _bap_uri(),
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": _iso_now(),
            "ttl": "PT30S",
        },
        "message": {"order": body.order or {}},
    }
    entry = {
        "id": f"out_{uuid.uuid4().hex[:12]}",
        "action": action,
        "payload": envelope,
        "created_at": int(time.time()),
        "status": "queued",
        "message_id": message_id,
        "transaction_id": transaction_id,
        "bpp_id": bpp_id,
        "bpp_uri": bpp_uri,
    }
    target = f"{bpp_uri}/{action}"
    staged = await _stage_outbox_before_dispatch(
        request, entry, destination=target
    )
    if staged.get("already_delivered"):
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "queued": False,
                    "dispatched": False,
                    "deduplicated": True,
                    "outbox_id": staged["public_id"],
                    "message_id": message_id,
                    "transaction_id": transaction_id,
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "target": target,
                },
            }
        )
    try:
        status, data, _ = await _signed_post(target, envelope)
    except HTTPException:
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error="signing/config",
            file_updates={"status": "error", "error": "signing/config"},
        )
        raise
    except Exception as exc:  # noqa: BLE001
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error=str(exc),
            file_updates={"status": "error", "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=f"ONDC BPP {action} failed: {exc}") from exc

    ack = None
    if isinstance(data, dict):
        ack = ((data.get("message") or {}).get("ack") or {}).get("status")
    dispatch_status = "sent" if status < 400 else "nack"
    if ack == "NACK":
        dispatch_status = "nack"
    await _complete_outbox_delivery(
        staged,
        delivered=dispatch_status == "sent",
        error=f"BPP returned HTTP {status} ack={ack}",
        file_updates={
            "status": dispatch_status,
            "http_status": status,
            "bpp_response": data,
        },
    )
    return JSONResponse(
        {
            "success": dispatch_status == "sent",
            "data": {
                "queued": False,
                "dispatched": True,
                "outbox_id": staged["public_id"],
                "message_id": message_id,
                "transaction_id": transaction_id,
                "bpp_id": bpp_id,
                "bpp_uri": bpp_uri,
                "http_status": status,
                "ack": ack,
                "target": target,
                "bpp_response": data,
                "note": f"Poll GET /api/ondc/inbox?action=on_{action} or /api/ondc/orders?transaction_id=…",
            },
        }
    )


@router.post("/api/ondc/select")
async def ondc_select(body: OrderActionBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "select", body)


@router.post("/api/ondc/init")
async def ondc_init(body: OrderActionBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "init", body)


@router.post("/api/ondc/confirm")
async def ondc_confirm(body: ConfirmBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "confirm", body)


def _queue_record(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe operational projection of a durable protocol record."""
    return {
        key: (
            value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, uuid.UUID)
            else value
        )
        for key, value in row.items()
    }


async def _persistent_records(
    request: Request,
    queue: str,
    *,
    transaction_id: str | None = None,
    action: str | None = None,
    state: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]] | None:
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        return None
    async with UnitOfWork(pool) as unit_of_work:
        rows = await ONDCRepository(unit_of_work).list_records(
            queue,  # type: ignore[arg-type]
            transaction_id=transaction_id,
            action=action,
            state=state,
            limit=limit,
        )
    return [_queue_record(row) for row in rows]


def _require_recovery_write_contract(request: Request) -> tuple[str, str]:
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    correlation_id = (request.headers.get("X-Correlation-ID") or "").strip()
    if not idempotency_key or not correlation_id:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key and X-Correlation-ID headers are required",
        )
    return idempotency_key, correlation_id


@router.get("/api/ondc/orders")
async def ondc_orders(
    request: Request, transaction_id: Optional[str] = None
) -> JSONResponse:
    outbox = await _persistent_records(
        request, "outbox", transaction_id=transaction_id, limit=500
    )
    if outbox is not None:
        inbox = await _persistent_records(
            request, "inbox", transaction_id=transaction_id, limit=500
        ) or []
        order_actions = {"select", "init", "confirm"}
        items = []
        for record in outbox:
            if record["action"] not in order_actions:
                continue
            envelope = record.get("envelope") or {}
            order = ((envelope.get("message") or {}).get("order") or {})
            items.append(
                {
                    "id": order.get("id") or f"pg_out_{record['outbox_id']}",
                    "transaction_id": record["transaction_id"],
                    "bpp_id": (envelope.get("context") or {}).get("bpp_id"),
                    "state": order.get("state") or record["state"],
                    "order": order,
                    "created_at": record["created_at"],
                    "delivery_state": record["state"],
                }
            )
        return JSONResponse(
            {"success": True, "data": {"items": items, "callbacks": inbox}}
        )
    return JSONResponse(
        {
            "success": True,
            "data": {
                "items": ondc_store.list_orders(transaction_id=transaction_id),
                "callbacks": (
                    ondc_store.callbacks_for_transaction(transaction_id)
                    if transaction_id
                    else []
                ),
            },
        }
    )


async def _ingest_callback(
    request: Request, action: str, body: dict[str, Any]
) -> JSONResponse:
    ctx = body.get("context") or {}
    normalized_action = action if action.startswith("on_") else f"on_{action}"
    transaction_id = str(ctx.get("transaction_id") or "").strip()
    message_id = str(ctx.get("message_id") or "").strip()
    subscriber_id = str(
        ctx.get("bpp_id") or ctx.get("bap_id") or ctx.get("subscriber_id") or ""
    ).strip()
    if not transaction_id or not message_id or not subscriber_id:
        return JSONResponse(
            {
                "message": {"ack": {"status": "NACK"}},
                "error": {
                    "type": "CORE-ERROR",
                    "code": "30000",
                    "message": (
                        "context.transaction_id, context.message_id, and a "
                        "subscriber identifier are required"
                    ),
                },
            },
            status_code=400,
        )

    persistence_pool = getattr(request.app.state, "persistence_pool", None)
    if persistence_pool is not None:
        correlation_id = (
            request.headers.get("X-Correlation-ID") or transaction_id
        ).strip()
        try:
            await persist_callback_before_ack(
                persistence_pool,
                subscriber_id=subscriber_id,
                transaction_id=transaction_id,
                message_id=message_id,
                action=normalized_action,
                correlation_id=correlation_id,
                raw_envelope=body,
                redacted_payload={"status": "received"},
            )
        except (
            CorrelationMismatch,
            EnvelopeCommitmentMismatch,
            ValueError,
        ) as exc:
            return JSONResponse(
                {
                    "message": {"ack": {"status": "NACK"}},
                    "error": {
                        "type": "CORE-ERROR",
                        "code": "30000",
                        "message": str(exc),
                    },
                },
                status_code=409,
            )
        except Exception:
            return JSONResponse(
                {
                    "message": {"ack": {"status": "NACK"}},
                    "error": {
                        "type": "DOMAIN-ERROR",
                        "code": "50000",
                        "message": "callback persistence unavailable",
                    },
                },
                status_code=503,
            )
        return JSONResponse({"message": {"ack": {"status": "ACK"}}})

    entry = {
        "id": f"in_{uuid.uuid4().hex[:12]}",
        "action": normalized_action,
        "payload": body,
        "received_at": int(time.time()),
        "transaction_id": transaction_id,
        "message_id": message_id,
        "bpp_id": subscriber_id,
    }
    ondc_store.append_inbox(entry)
    return JSONResponse({"message": {"ack": {"status": "ACK"}}})


@router.post("/api/ondc/callback/{action}")
async def ondc_callback_api(
    action: str, request: Request, body: dict[str, Any]
) -> JSONResponse:
    return await _ingest_callback(request, action, body)


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
            return await _ingest_callback(request, f"on_{action}", body)

        async def _np(role: str, request: Request, action: str = act) -> JSONResponse:
            if role not in {"buyer", "seller"}:
                raise HTTPException(status_code=404, detail="role must be buyer|seller")
            body = await request.json()
            return await _ingest_callback(request, f"on_{action}", body)

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
async def ondc_outbox(
    request: Request,
    state: Optional[str] = None,
    transaction_id: Optional[str] = None,
) -> JSONResponse:
    items = await _persistent_records(
        request, "outbox", state=state, transaction_id=transaction_id
    )
    if items is not None:
        return JSONResponse({"success": True, "data": {"items": items}})
    return JSONResponse({"success": True, "data": {"items": ondc_store.list_outbox()}})


@router.get("/api/ondc/inbox")
async def ondc_inbox(
    request: Request,
    action: Optional[str] = None,
    state: Optional[str] = None,
    transaction_id: Optional[str] = None,
) -> JSONResponse:
    items = await _persistent_records(
        request,
        "inbox",
        action=action,
        state=state,
        transaction_id=transaction_id,
    )
    if items is not None:
        return JSONResponse({"success": True, "data": {"items": items}})
    return JSONResponse(
        {"success": True, "data": {"items": ondc_store.list_inbox(action=action)}}
    )


@router.get("/api/ondc/catalogs")
async def ondc_catalogs(request: Request, transaction_id: str) -> JSONResponse:
    if not transaction_id.strip():
        raise HTTPException(status_code=400, detail="transaction_id required")
    records = await _persistent_records(
        request,
        "inbox",
        transaction_id=transaction_id.strip(),
        action="on_search",
        limit=500,
    )
    if records is None:
        items = ondc_store.catalogs_for_transaction(transaction_id.strip())
    else:
        items = []
        for record in records:
            message = (record.get("envelope") or {}).get("message") or {}
            providers = ((message.get("catalog") or {}).get("bpp/providers") or [])
            items.extend(providers)
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


@router.post("/api/ondc/outbox/drain")
async def drain_ondc_outbox(request: Request, body: OutboxDrainBody) -> JSONResponse:
    """Lease and deliver due intents; safe to call after process restart."""
    _require_recovery_write_contract(request)
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        raise HTTPException(status_code=409, detail="PostgreSQL persistence is required")
    async with UnitOfWork(pool) as unit_of_work:
        claimed = await ONDCRepository(unit_of_work).claim_outbox(
            worker_id=body.worker_id,
            lease_seconds=body.lease_seconds,
            limit=body.limit,
        )
    results: list[dict[str, Any]] = []
    for record in claimed:
        delivered = False
        error = ""
        try:
            status, response, _ = await _signed_post(
                record["destination"], record["envelope"]
            )
            ack = (
                ((response.get("message") or {}).get("ack") or {}).get("status")
                if isinstance(response, dict)
                else None
            )
            delivered = status < 400 and ack != "NACK"
            error = f"destination returned HTTP {status} ack={ack}"
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        async with UnitOfWork(pool) as unit_of_work:
            repository = ONDCRepository(unit_of_work)
            if delivered:
                updated = await repository.mark_delivered(
                    "outbox", record["outbox_id"], record["lease_token"]
                )
            else:
                updated = await repository.schedule_retry(
                    "outbox",
                    record["outbox_id"],
                    record["lease_token"],
                    error=error,
                )
        results.append(_queue_record(updated))
    return JSONResponse(
        {"success": True, "data": {"claimed": len(claimed), "items": results}}
    )


@router.post("/api/ondc/{queue}/dead-letter/{record_id}/requeue")
async def requeue_ondc_dead_letter(
    queue: str,
    record_id: int,
    request: Request,
    body: DeadLetterRecoveryBody,
) -> JSONResponse:
    _require_recovery_write_contract(request)
    if queue not in {"inbox", "outbox"}:
        raise HTTPException(status_code=404, detail="queue must be inbox|outbox")
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        raise HTTPException(status_code=409, detail="PostgreSQL persistence is required")
    async with UnitOfWork(pool) as unit_of_work:
        updated = await ONDCRepository(unit_of_work).requeue_dead_letter(
            queue,  # type: ignore[arg-type]
            record_id,
            event_commitment=body.event_commitment,
        )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="dead letter not found or event commitment did not match",
        )
    return JSONResponse({"success": True, "data": _queue_record(updated)})
