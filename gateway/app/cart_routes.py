"""Buyer SPA cart/billing persist.

Canonical billing persist is ``/api/cart/buyer/{session_id}`` — that is the
path the live Buyer SPA already PATCHes. CommerceV1 has no buyer profile
resource; ``GET /api/commerce/v1/payments/config`` is the payment-rail read
and must be called on the gateway host (or proxied). Same-origin
``ondcbuyer.aadharcha.in/api/...`` without a proxy returns the SPA HTML shell.

Session-id gated. Missing profile is 200 draft/upsert. Collection without
a session id is 401 JSON, never 404.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import cart_sessions

router = APIRouter(prefix="/api/cart", tags=["buyer-cart"])


class CartItemBody(BaseModel):
    sessionId: Optional[str] = None
    session_id: Optional[str] = None
    item: dict[str, Any] = Field(default_factory=dict)
    quantity: int = 1


class CartQuantityBody(BaseModel):
    sessionId: Optional[str] = None
    session_id: Optional[str] = None
    quantity: int = 1


class BuyerBody(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    taxId: Optional[str] = None
    tax_id: Optional[str] = None
    line1: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postalCode: Optional[str] = None
    pincode: Optional[str] = None
    pin: Optional[str] = None
    country: Optional[str] = None


def _session_id(*values: Optional[str], missing_status: int = 422) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            if len(text) > 160:
                raise HTTPException(status_code=422, detail="session_id is invalid")
            return text
    raise HTTPException(status_code=missing_status, detail="session_id is required")


def _payload(session: dict[str, Any]) -> dict[str, Any]:
    return {"session": session, "success": True}


async def _upsert_buyer(request: Request, session_id: str, body: BuyerBody) -> dict[str, Any]:
    resolved = _session_id(session_id, missing_status=401)
    session = await cart_sessions.upsert_buyer(
        request, resolved, body.model_dump(exclude_none=True)
    )
    return _payload(session)


@router.get("/buyer")
async def get_cart_buyer_collection() -> dict[str, Any]:
    raise HTTPException(status_code=401, detail="session_id is required")


@router.put("/buyer")
async def put_cart_buyer_collection() -> dict[str, Any]:
    raise HTTPException(status_code=401, detail="session_id is required")


@router.patch("/buyer")
async def patch_cart_buyer_collection() -> dict[str, Any]:
    raise HTTPException(status_code=401, detail="session_id is required")


@router.get("/buyer/{session_id}")
async def get_cart_buyer(session_id: str, request: Request) -> dict[str, Any]:
    resolved = _session_id(session_id, missing_status=401)
    return _payload(await cart_sessions.load_session(request, resolved))


@router.put("/buyer/{session_id}")
async def put_cart_buyer(
    session_id: str, request: Request, body: BuyerBody = BuyerBody()
) -> dict[str, Any]:
    return await _upsert_buyer(request, session_id, body)


@router.patch("/buyer/{session_id}")
async def patch_cart_buyer(
    session_id: str, request: Request, body: BuyerBody = BuyerBody()
) -> dict[str, Any]:
    return await _upsert_buyer(request, session_id, body)


@router.get("")
async def get_cart(request: Request, sessionId: Optional[str] = None) -> dict[str, Any]:
    session_id = _session_id(sessionId, missing_status=401)
    return _payload(await cart_sessions.load_session(request, session_id))


@router.post("")
async def add_cart_item(request: Request, body: CartItemBody) -> dict[str, Any]:
    session_id = _session_id(body.sessionId, body.session_id, missing_status=401)
    try:
        session = await cart_sessions.add_item(
            request, session_id, body.item, int(body.quantity)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _payload(session)


@router.put("/{item_id}")
async def set_cart_item(
    item_id: str, request: Request, body: CartQuantityBody
) -> dict[str, Any]:
    session_id = _session_id(body.sessionId, body.session_id, missing_status=401)
    try:
        session = await cart_sessions.set_item_quantity(
            request, session_id, item_id, int(body.quantity)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="cart item not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _payload(session)


@router.delete("/{item_id}")
async def delete_cart_item(
    item_id: str, request: Request, sessionId: Optional[str] = None
) -> dict[str, Any]:
    session_id = _session_id(sessionId, missing_status=401)
    session = await cart_sessions.set_item_quantity(request, session_id, item_id, 0)
    return _payload(session)
