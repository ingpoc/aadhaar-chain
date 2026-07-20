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
    # Never widen a specific miss into the full catalog (TV must not return oil).
    # Only browse-style empty queries may list the whole published inventory.
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
                "delivery_areas": list(item.get("delivery_areas") or []),
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
                "name": "Sampoorna Groceries",
                "short_desc": "Grocery seller on ONDC",
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
                    "name": "Sampoorna Groceries",
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


# --- select / init / confirm ---

def _order_from_request(body: dict[str, Any]) -> dict[str, Any]:
    message = body.get("message") or {}
    order = dict(message.get("order") or {})
    # Resolve requested ids against the published Seller catalog. A bare ACK
    # with an unpriced item is not a useful select/init/confirm proof.
    providers = build_catalog_providers(query="")
    catalog_items = (providers[0].get("items") if providers else []) or []
    by_id = {i.get("id"): i for i in catalog_items}
    requested = order.get("items") or message.get("items") or order.get("item_ids") or []
    items: list[dict[str, Any]] = []
    if isinstance(requested, list):
        for row in requested:
            if isinstance(row, str):
                found = by_id.get(row)
                if found:
                    items.append({**found, "quantity": {"count": "1"}})
            elif isinstance(row, dict):
                iid = row.get("id") or row.get("item_id")
                found = by_id.get(iid) if iid else None
                qty = row.get("quantity") or {"count": "1"}
                if found:
                    items.append(
                        {
                            **found,
                            **row,
                            "quantity": qty if isinstance(qty, dict) else {"count": str(qty)},
                        }
                    )
                elif iid:
                    items.append({"id": iid, "quantity": qty if isinstance(qty, dict) else {"count": "1"}})
    if not items and catalog_items:
        items = [{**catalog_items[0], "quantity": {"count": "1"}}]
    total = 0.0
    for item in items:
        try:
            quantity = float(((item.get("quantity") or {}).get("count") or 1))
            total += float(((item.get("price") or {}).get("value") or 0)) * quantity
        except (TypeError, ValueError):
            pass
    price_str = f"{total:.2f}"
    return {
        **order,
        "provider": order.get("provider")
        or {"id": PROVIDER_ID, "descriptor": {"name": "Sampoorna Groceries"}},
        "items": items,
        "quote": order.get("quote")
        or {
            "price": {"currency": "INR", "value": price_str},
            "breakup": [
                {
                    "title": (item.get("descriptor") or {}).get("name") or item.get("id"),
                    "@ondc/org/item_id": item.get("id"),
                    "price": item.get("price") or {"currency": "INR", "value": "0.00"},
                }
                for item in items
            ],
        },
        "fulfillments": [{"id": "1", "type": "Delivery", "tracking": False}],
        "billing": order.get("billing")
        or {
            "name": "AgentGuard Buyer",
            "address": {
                "locality": "Bengaluru",
                "city": "Bengaluru",
                "area_code": "560001",
                "state": "KA",
            },
            "phone": "9999999999",
        },
        "payment": order.get("payment")
        or {
            "type": "ON-ORDER",
            "collected_by": "BPP",
            "@ondc/org/buyer_app_finder_fee_type": "percent",
            "@ondc/org/buyer_app_finder_fee_amount": "0",
            "status": "NOT-PAID",
        },
    }


async def _post_on_action(action: str, request_body: dict[str, Any]) -> None:
    """ACK path already returned; post on_select / on_init / on_confirm to bap_uri."""
    if not _bpp_ready():
        logger.warning("BPP on_%s skipped — seller keys / ONDC_ENABLED not ready", action)
        return
    ctx = request_body.get("context") or {}
    bap_uri = str(ctx.get("bap_uri") or "").rstrip("/")
    if not bap_uri:
        logger.warning("BPP on_%s skipped — missing bap_uri", action)
        return
    order = _order_from_request(request_body)
    if action == "confirm":
        order = {
            **order,
            "id": f"ord_{uuid.uuid4().hex[:12]}",
            "state": "Accepted",
            "payment": {
                **(order.get("payment") or {}),
                "status": "PAID",
                "type": "ON-ORDER",
            },
        }
        from app import ondc_store

        ondc_store.append_order(
            {
                "id": order["id"],
                "transaction_id": ctx.get("transaction_id"),
                "bpp_id": _bpp_id(),
                "state": order["state"],
                "order": order,
                "created_at": int(time.time()),
            }
        )
    elif action == "init":
        order = {**order, "payment": {**(order.get("payment") or {}), "status": "NOT-PAID"}}
    message_id = str(uuid.uuid4())
    on_action = f"on_{action}"
    envelope = {
        "context": {
            "domain": ctx.get("domain") or DEFAULT_DOMAIN,
            "action": on_action,
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
        "message": {"order": order},
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
    url = f"{bap_uri}/{on_action}"
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
        logger.info("BPP %s → %s status=%s", on_action, url, resp.status_code)
    except Exception:  # noqa: BLE001
        logger.exception("BPP %s dispatch failed to %s", on_action, url)


async def handle_bpp_order_action(
    action: str, body: dict[str, Any], background: BackgroundTasks
) -> JSONResponse:
    if action not in {"select", "init", "confirm"}:
        raise HTTPException(status_code=404, detail=f"unsupported BPP action: {action}")
    if not getattr(settings, "ondc_enabled", False):
        raise HTTPException(status_code=503, detail="ONDC_ENABLED=false")
    background.add_task(_post_on_action, action, body)
    return JSONResponse({"message": {"ack": {"status": "ACK"}}})


@router.post("/ondc/np/seller/select")
async def np_seller_select(request: Request, background: BackgroundTasks) -> JSONResponse:
    return await handle_bpp_order_action("select", await request.json(), background)


@router.post("/ondc/np/seller/init")
async def np_seller_init(request: Request, background: BackgroundTasks) -> JSONResponse:
    return await handle_bpp_order_action("init", await request.json(), background)


@router.post("/ondc/np/seller/confirm")
async def np_seller_confirm(request: Request, background: BackgroundTasks) -> JSONResponse:
    return await handle_bpp_order_action("confirm", await request.json(), background)


@router.post("/ondc/select")
async def root_select(request: Request, background: BackgroundTasks) -> JSONResponse:
    return await handle_bpp_order_action("select", await request.json(), background)


@router.post("/ondc/init")
async def root_init(request: Request, background: BackgroundTasks) -> JSONResponse:
    return await handle_bpp_order_action("init", await request.json(), background)


@router.post("/ondc/confirm")
async def root_confirm(request: Request, background: BackgroundTasks) -> JSONResponse:
    return await handle_bpp_order_action("confirm", await request.json(), background)
