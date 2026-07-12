"""ONDC BPP (Seller) — receive search, reply on_search from published catalog.

Catalog source: published rows in demo-commerce state (DATA_DIR) — no mock grocery.
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from app.commerce_demo import load_state, search_items
from app.ondc_crypto import create_authorization_header, load_ed25519_private_pem, minify_json
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ondc-bpp"])

CORE_VERSION = "1.2.0"
DEFAULT_DOMAIN = "ONDC:RET10"
PROVIDER_ID = "aadhaar-seller-isn"


def _seller_paths() -> dict[str, Path]:
    from app.ondc_onboard_routes import _role_paths

    return _role_paths("seller")


def _seller_uk_id() -> Optional[str]:
    env_uk = getattr(settings, "ondc_seller_unique_key_id", None)
    if env_uk:
        return str(env_uk).strip() or None
    # Prefer dedicated seller env file / portal uk
    import os

    raw = (os.environ.get("ONDC_SELLER_UNIQUE_KEY_ID") or "").strip()
    if raw:
        return raw
    paths = _seller_paths()
    if paths["uk_id"].is_file():
        value = paths["uk_id"].read_text(encoding="utf-8").strip()
        if value:
            return value
    if paths["meta"].is_file():
        try:
            import json

            meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
            uk = meta.get("unique_key_id")
            if uk:
                return str(uk).strip()
        except Exception:  # noqa: BLE001
            pass
    return None


def _bpp_id() -> str:
    return (
        getattr(settings, "ondc_bpp_id", None)
        or getattr(settings, "ondc_seller_subscriber_id", None)
        or "ondcseller.aadharcha.in"
    )


def _bpp_uri() -> str:
    configured = getattr(settings, "ondc_bpp_uri", None)
    if configured:
        return str(configured).rstrip("/")
    return f"https://{_bpp_id()}/ondc"


def _seller_signing_pem() -> Optional[Path]:
    configured = getattr(settings, "ondc_seller_signing_private_key_path", None)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path
    paths = _seller_paths()
    if paths["signing_pem"].is_file():
        return paths["signing_pem"]
    return None


def _bpp_ready() -> bool:
    return bool(
        getattr(settings, "ondc_enabled", False)
        and _seller_uk_id()
        and _seller_signing_pem() is not None
    )


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def _intent_query(search_body: dict[str, Any]) -> str:
    intent = ((search_body.get("message") or {}).get("intent")) or {}
    item = intent.get("item") or {}
    descriptor = item.get("descriptor") or {}
    return str(descriptor.get("name") or "").strip()


def build_catalog_providers(*, query: str = "") -> list[dict[str, Any]]:
    """Map published demo-commerce items → Beckn Retail providers/items."""
    result = search_items(query or None)
    rows = result.get("items") or []
    # If filtered empty but we have any published inventory, still return all
    # when query is generic (gateway often sends broad grocery searches).
    if not rows and query:
        rows = (search_items(None).get("items") or [])
    state = load_state()
    beckn_items: list[dict[str, Any]] = []
    for item in rows:
        item_id = item.get("item_id")
        qty = int(state.inventory.get(item_id, 0) or 0)
        price = f"{int(item.get('price_inr') or 0)}.00"
        beckn_items.append(
            {
                "id": item_id,
                "descriptor": {
                    "name": item.get("title") or item_id,
                    "code": item_id,
                    "short_desc": item.get("description") or item.get("title") or "",
                    "long_desc": item.get("description") or item.get("title") or "",
                },
                "price": {"currency": "INR", "value": price, "maximum_value": price},
                "quantity": {
                    "available": {"count": str(max(qty, 1))},
                    "maximum": {"count": str(max(qty, 99))},
                },
                "category_id": "Foodgrains",
                "fulfillment_id": "1",
                "location_id": "L1",
                "@ondc/org/returnable": False,
                "@ondc/org/cancellable": True,
                "@ondc/org/available_on_cod": False,
                "@ondc/org/time_to_ship": "P1D",
            }
        )
    if not beckn_items:
        return []
    return [
        {
            "id": PROVIDER_ID,
            "descriptor": {
                "name": "AadhaarChain AgentGuard Seller",
                "short_desc": "PreProd ISN seller for AgentGuard Token Nxt demo",
            },
            "fulfillments": [{"id": "1", "type": "Delivery"}],
            "locations": [
                {
                    "id": "L1",
                    "gps": "12.9715987,77.5945627",
                    "address": {
                        "locality": "Bengaluru",
                        "city": "Bengaluru",
                        "area_code": "560001",
                        "state": "KA",
                    },
                }
            ],
            "items": beckn_items,
        }
    ]


async def _post_on_search(search_body: dict[str, Any]) -> None:
    if not _bpp_ready():
        logger.warning("BPP on_search skipped — seller keys / ONDC_ENABLED not ready")
        return
    ctx = search_body.get("context") or {}
    bap_uri = str(ctx.get("bap_uri") or "").rstrip("/")
    if not bap_uri:
        logger.warning("BPP on_search skipped — missing bap_uri")
        return
    query = _intent_query(search_body)
    providers = build_catalog_providers(query=query)
    if not providers:
        logger.info("BPP on_search — no published items; sending empty catalog")
    message_id = str(uuid.uuid4())
    envelope = {
        "context": {
            "domain": ctx.get("domain") or DEFAULT_DOMAIN,
            "action": "on_search",
            "country": ctx.get("country") or "IND",
            "city": ctx.get("city") or "std:080",
            "core_version": ctx.get("core_version") or CORE_VERSION,
            "bap_id": ctx.get("bap_id"),
            "bap_uri": bap_uri,
            "bpp_id": _bpp_id(),
            "bpp_uri": _bpp_uri(),
            "transaction_id": ctx.get("transaction_id"),
            "message_id": message_id,
            "timestamp": _iso_now(),
            "ttl": ctx.get("ttl") or "PT30S",
        },
        "message": {
            "catalog": {
                "bpp/descriptor": {
                    "name": "AadhaarChain AgentGuard Seller",
                },
                "bpp/providers": providers,
                "providers": providers,
            }
        },
    }
    uk = _seller_uk_id()
    pem = _seller_signing_pem()
    if not uk or pem is None:
        return
    private_key = load_ed25519_private_pem(pem.read_bytes())
    body_str = minify_json(envelope)
    auth = create_authorization_header(
        body_str,
        subscriber_id=_bpp_id(),
        unique_key_id=uk,
        private_key=private_key,
    )
    url = f"{bap_uri}/on_search"
    try:
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
        logger.info("BPP on_search → %s status=%s", url, resp.status_code)
    except Exception:  # noqa: BLE001
        logger.exception("BPP on_search dispatch failed to %s", url)


async def handle_bpp_search(body: dict[str, Any], background: BackgroundTasks) -> JSONResponse:
    if not getattr(settings, "ondc_enabled", False):
        raise HTTPException(status_code=503, detail="ONDC_ENABLED=false")
    background.add_task(_post_on_search, body)
    return JSONResponse({"message": {"ack": {"status": "ACK"}}})


@router.post("/ondc/np/seller/search")
async def np_seller_search(request: Request, background: BackgroundTasks) -> JSONResponse:
    body = await request.json()
    return await handle_bpp_search(body, background)


@router.post("/ondc/search")
async def root_search(request: Request, background: BackgroundTasks) -> JSONResponse:
    """BPP search when called on Seller subscriber URI (after Vercel rewrite use /ondc/np/seller/search)."""
    body = await request.json()
    return await handle_bpp_search(body, background)


@router.get("/api/ondc/bpp/status")
async def bpp_status() -> JSONResponse:
    published = search_items(None)
    return JSONResponse(
        {
            "success": True,
            "data": {
                "ready": _bpp_ready(),
                "bpp_id": _bpp_id(),
                "bpp_uri": _bpp_uri(),
                "signing_key_present": _seller_signing_pem() is not None,
                "unique_key_id": _seller_uk_id(),
                "published_item_count": published.get("count") or 0,
            },
        }
    )


@router.post("/api/ondc/bpp/ensure-demo-item")
async def bpp_ensure_demo_item() -> JSONResponse:
    """Idempotent publish of a distinctive PreProd marker SKU (no mock UI catalog)."""
    from app.commerce_demo import create_item, publish_item

    marker_title = "AgentGuard PreProd Atta 1kg"
    state = load_state()
    existing = next(
        (i for i in state.items.values() if i.get("title") == marker_title and i.get("status") == "published"),
        None,
    )
    if existing:
        return JSONResponse({"success": True, "data": {"item": existing, "created": False}})
    created = create_item(
        {
            "title": marker_title,
            "description": "Network-visible Seller catalog item for PreProd Buyer discovery",
            "price_inr": 89,
            "inventory": 25,
            "seller_id": "ondcseller.aadharcha.in",
        },
        idempotency_key="bpp-ensure-agentguard-atta",
    )
    published = publish_item(
        created["item"]["item_id"],
        idempotency_key="bpp-ensure-agentguard-atta:publish",
    )
    return JSONResponse({"success": True, "data": {"item": published["item"], "created": True}})
