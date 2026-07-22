"""HTTP routes for the local AgentGuard commerce demo."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app import commerce_demo
from app.commerce_compat import CommerceCompatibilityAdapter
from app.models import ApiResponse
from app.session_auth import SESSION_COOKIE_NAME, parse_session_token
from config import get_runtime_mode

router = APIRouter(prefix="/api/demo-commerce", tags=["demo-commerce"])


def _require_test_fixture_mode() -> None:
    """Keep state-seeding endpoints out of staging and production runtimes."""
    if get_runtime_mode() != "demo":
        raise HTTPException(status_code=404, detail="Not found")


def _session_principal(request: Request, audience: str) -> str:
    session = parse_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))
    if not session or not session.get("principal_id"):
        raise HTTPException(status_code=401, detail="Authenticated principal required.")
    if session.get("aud") != audience:
        raise HTTPException(status_code=403, detail="Session audience mismatch.")
    return str(session["principal_id"])


def _compat(request: Request) -> CommerceCompatibilityAdapter | None:
    pool = getattr(request.app.state, "persistence_pool", None)
    return CommerceCompatibilityAdapter(pool) if pool is not None else None


async def _owned_order(
    request: Request, order_id: str, principal_id: str, owner_field: str
) -> dict[str, Any]:
    compat = _compat(request)
    if compat is not None:
        filters = (
            {"principal_id": principal_id}
            if owner_field == "buyer_id"
            else {"seller_id": principal_id}
        )
        return {"order": await compat.get_order(order_id, **filters)}
    try:
        result = commerce_demo.get_order(order_id)
    except Exception as exc:
        _handle_error(exc)
    order = result["order"]
    if order.get(owner_field) != principal_id:
        raise HTTPException(status_code=404, detail="Order not found.")
    return result


async def _owned_item(
    request: Request, item_id: str, principal_id: str
) -> dict[str, Any]:
    compat = _compat(request)
    if compat is not None:
        item = await compat.get_item(item_id, seller_id=principal_id)
        return {"item": item, "inventory": item["inventory"]}
    try:
        result = commerce_demo.get_item(item_id)
    except Exception as exc:
        _handle_error(exc)
    if result["item"].get("seller_id") != principal_id:
        raise HTTPException(status_code=404, detail="Item not found.")
    return result


fixture_router = APIRouter(
    prefix="/test-fixtures",
    dependencies=[Depends(_require_test_fixture_mode)],
)


class CommerceBody(BaseModel):
    idempotency_key: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)


class ItemBody(BaseModel):
    idempotency_key: Optional[str] = None
    title: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    price_inr: int = Field(0, ge=0)
    inventory: int = Field(0, ge=0)
    seller_id: Optional[str] = None
    seller_name: Optional[str] = None
    category_id: Optional[str] = None
    delivery_estimate: Optional[str] = None
    return_policy: Optional[str] = None
    image_url: Optional[str] = None
    image_caption: Optional[str] = None
    delivery_areas: Optional[list[str]] = None


class OrderBody(BaseModel):
    idempotency_key: Optional[str] = None
    item_id: str
    quantity: int = Field(1, ge=1)
    buyer_id: Optional[str] = None
    payment_mode: str = "success"
    item_title: Optional[str] = None
    delivery_address: Optional[dict[str, Any]] = None


class TransitionBody(BaseModel):
    idempotency_key: Optional[str] = None
    status: str


class IssueBody(BaseModel):
    idempotency_key: Optional[str] = None
    reason: Optional[str] = None
    description: Optional[str] = None


def _idem(body_key: Optional[str], header_key: Optional[str]) -> Optional[str]:
    return body_key or header_key


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc


@fixture_router.post("/cleanup", response_model=ApiResponse)
async def cleanup_test_fixtures(
    request: Request, body: CommerceBody = CommerceBody()
) -> ApiResponse:
    requested = body.data.get("order_ids") or []
    explicit_order_ids = {
        str(order_id) for order_id in requested if isinstance(order_id, str) and order_id.strip()
    }
    requested_items = body.data.get("item_ids") or []
    explicit_item_ids = {
        str(item_id) for item_id in requested_items if isinstance(item_id, str) and item_id.strip()
    }
    compat = _compat(request)
    if compat is not None:
        result = await compat.cleanup(
            order_ids=explicit_order_ids, item_ids=explicit_item_ids
        )
    else:
        result = commerce_demo.cleanup_test_artifacts(
            explicit_order_ids=explicit_order_ids,
            explicit_item_ids=explicit_item_ids,
            include_discovered=not (explicit_order_ids or explicit_item_ids),
        )
    return ApiResponse(success=True, message="Test fixtures removed", data=result)


@fixture_router.post("/seller/items", response_model=ApiResponse)
async def create_seller_item(
    request: Request,
    body: ItemBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        payload = body.model_dump(exclude_none=True)
        compat = _compat(request)
        result = (
            await compat.create_item(payload)
            if compat is not None
            else commerce_demo.create_item(
                payload,
                idempotency_key=_idem(body.idempotency_key, idempotency_key),
            )
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Item created", data=result)


@fixture_router.patch("/seller/items/{item_id}", response_model=ApiResponse)
async def update_seller_item(
    item_id: str, request: Request, body: dict[str, Any]
) -> ApiResponse:
    try:
        compat = _compat(request)
        result = (
            await compat.update_item(item_id, body)
            if compat is not None
            else commerce_demo.update_item(item_id, body)
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Item updated", data=result)


@fixture_router.post("/seller/items/{item_id}/publish", response_model=ApiResponse)
async def publish_seller_item(
    item_id: str,
    request: Request,
    body: CommerceBody = CommerceBody(),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        compat = _compat(request)
        result = (
            await compat.publish_item(item_id)
            if compat is not None
            else commerce_demo.publish_item(
                item_id,
                idempotency_key=_idem(body.idempotency_key, idempotency_key),
            )
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Item published", data=result)


@router.get("/buyer/search", response_model=ApiResponse)
async def search_items(request: Request, q: Optional[str] = None) -> ApiResponse:
    compat = _compat(request)
    data = (
        await compat.list_items(query=q, published_only=True)
        if compat is not None
        else commerce_demo.search_items(q)
    )
    return ApiResponse(success=True, message="Items", data=data)


@router.get("/buyer/items/{item_id}", response_model=ApiResponse)
async def get_item(item_id: str, request: Request) -> ApiResponse:
    try:
        compat = _compat(request)
        if compat is not None:
            item = await compat.get_item(item_id)
            result = {"item": item, "inventory": item["inventory"]}
        else:
            result = commerce_demo.get_item(item_id)
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Item", data=result)


@router.get("/seller/items", response_model=ApiResponse)
async def list_seller_items(request: Request) -> ApiResponse:
    principal_id = _session_principal(request, "ondcseller")
    compat = _compat(request)
    data = (
        await compat.list_items(seller_id=principal_id)
        if compat is not None
        else commerce_demo.list_seller_items(principal_id)
    )
    return ApiResponse(success=True, message="Items", data=data)


@router.get("/seller/items/{item_id}", response_model=ApiResponse)
async def get_seller_item(item_id: str, request: Request) -> ApiResponse:
    principal_id = _session_principal(request, "ondcseller")
    return ApiResponse(
        success=True,
        message="Item",
        data=await _owned_item(request, item_id, principal_id),
    )


@fixture_router.post("/buyer/orders", response_model=ApiResponse)
async def create_buyer_order(
    request: Request,
    body: OrderBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        payload = body.model_dump(exclude_none=True)
        effective_key = _idem(body.idempotency_key, idempotency_key)
        compat = _compat(request)
        result = (
            await compat.create_order(payload, idempotency_key=effective_key)
            if compat is not None
            else commerce_demo.create_order(payload, idempotency_key=effective_key)
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Order created", data=result)


@router.get("/buyer/orders", response_model=ApiResponse)
async def list_buyer_orders(request: Request) -> ApiResponse:
    principal_id = _session_principal(request, "ondcbuyer")
    compat = _compat(request)
    data = (
        await compat.list_orders(principal_id=principal_id)
        if compat is not None
        else commerce_demo.list_buyer_orders(principal_id)
    )
    return ApiResponse(success=True, message="Orders", data=data)


@router.get("/buyer/orders/{order_id}", response_model=ApiResponse)
async def get_buyer_order(order_id: str, request: Request) -> ApiResponse:
    principal_id = _session_principal(request, "ondcbuyer")
    result = await _owned_order(request, order_id, principal_id, "buyer_id")
    return ApiResponse(success=True, message="Order", data=result)


@router.get("/seller/orders", response_model=ApiResponse)
async def list_seller_orders(request: Request) -> ApiResponse:
    principal_id = _session_principal(request, "ondcseller")
    compat = _compat(request)
    data = (
        await compat.list_orders(seller_id=principal_id)
        if compat is not None
        else commerce_demo.list_seller_orders(principal_id)
    )
    return ApiResponse(success=True, message="Orders", data=data)


@router.get("/seller/orders/{order_id}", response_model=ApiResponse)
async def get_seller_order(order_id: str, request: Request) -> ApiResponse:
    principal_id = _session_principal(request, "ondcseller")
    result = await _owned_order(request, order_id, principal_id, "seller_id")
    return ApiResponse(success=True, message="Order", data=result)


@fixture_router.post("/seller/orders/{order_id}/transition", response_model=ApiResponse)
async def transition_order(
    order_id: str,
    request: Request,
    body: TransitionBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        compat = _compat(request)
        result = (
            await compat.transition_order(order_id, body.status)
            if compat is not None
            else commerce_demo.transition_order(
                order_id,
                body.status,
                idempotency_key=_idem(body.idempotency_key, idempotency_key),
            )
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Order transitioned", data=result)


@fixture_router.post("/buyer/orders/{order_id}/issues", response_model=ApiResponse)
async def create_issue(
    order_id: str,
    request: Request,
    body: IssueBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        payload = body.model_dump(exclude_none=True)
        compat = _compat(request)
        result = (
            await compat.create_issue(order_id, payload)
            if compat is not None
            else commerce_demo.create_issue(
                order_id,
                payload,
                idempotency_key=_idem(body.idempotency_key, idempotency_key),
            )
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Issue created", data=result)


@router.get("/buyer/issues", response_model=ApiResponse)
async def list_buyer_issues(request: Request, order_id: Optional[str] = None) -> ApiResponse:
    principal_id = _session_principal(request, "ondcbuyer")
    compat = _compat(request)
    if compat is not None:
        if order_id:
            await _owned_order(request, order_id, principal_id, "buyer_id")
        data = await compat.list_issues(
            principal_id=principal_id, order_id=order_id
        )
        return ApiResponse(success=True, message="Issues", data=data)
    if order_id:
        await _owned_order(request, order_id, principal_id, "buyer_id")
    rows = commerce_demo.list_buyer_issues(order_id)["issues"]
    if not order_id:
        rows = [
            issue
            for issue in rows
            if commerce_demo.get_order(str(issue["order_id"]))["order"].get("buyer_id") == principal_id
        ]
    return ApiResponse(success=True, message="Issues", data={"issues": rows, "count": len(rows)})


@router.get("/seller/issues", response_model=ApiResponse)
async def list_seller_issues(request: Request) -> ApiResponse:
    principal_id = _session_principal(request, "ondcseller")
    compat = _compat(request)
    if compat is not None:
        return ApiResponse(
            success=True,
            message="Issues",
            data=await compat.list_issues(seller_id=principal_id),
        )
    rows = [
        issue
        for issue in commerce_demo.list_seller_issues()["issues"]
        if commerce_demo.get_order(str(issue["order_id"]))["order"].get("seller_id") == principal_id
    ]
    return ApiResponse(success=True, message="Issues", data={"issues": rows, "count": len(rows)})


@fixture_router.post("/seller/issues/{issue_id}/respond", response_model=ApiResponse)
async def respond_issue(
    issue_id: str, request: Request, body: dict[str, Any]
) -> ApiResponse:
    try:
        compat = _compat(request)
        result = (
            await compat.respond_issue(issue_id, body)
            if compat is not None
            else commerce_demo.respond_issue(issue_id, body)
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Issue responded", data=result)


@fixture_router.post("/seller/issues/{issue_id}/remedy", response_model=ApiResponse)
async def remedy_issue(
    issue_id: str,
    request: Request,
    body: CommerceBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    payload = {**body.data, "issue_id": issue_id}
    try:
        compat = _compat(request)
        result = (
            await compat.remedy_issue(issue_id, payload)
            if compat is not None
            else commerce_demo.propose_remedy(
                issue_id,
                payload,
                idempotency_key=_idem(body.idempotency_key, idempotency_key),
            )
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Remedy promised", data=result)


@fixture_router.get("/buyer/orders", response_model=ApiResponse)
async def list_fixture_buyer_orders(
    request: Request, buyer_id: Optional[str] = None
) -> ApiResponse:
    compat = _compat(request)
    data = (
        await compat.list_orders(principal_id=buyer_id)
        if compat is not None
        else commerce_demo.list_buyer_orders(buyer_id)
    )
    return ApiResponse(success=True, message="Orders", data=data)


@fixture_router.get("/buyer/orders/{order_id}", response_model=ApiResponse)
async def get_fixture_buyer_order(order_id: str, request: Request) -> ApiResponse:
    try:
        compat = _compat(request)
        result = (
            {"order": await compat.get_order(order_id)}
            if compat is not None
            else commerce_demo.get_order(order_id)
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Order", data=result)


@fixture_router.get("/seller/orders", response_model=ApiResponse)
async def list_fixture_seller_orders(
    request: Request, seller_id: Optional[str] = None
) -> ApiResponse:
    compat = _compat(request)
    data = (
        await compat.list_orders(seller_id=seller_id)
        if compat is not None
        else commerce_demo.list_seller_orders(seller_id)
    )
    return ApiResponse(success=True, message="Orders", data=data)


@fixture_router.get("/buyer/issues", response_model=ApiResponse)
async def list_fixture_buyer_issues(
    request: Request, order_id: Optional[str] = None
) -> ApiResponse:
    compat = _compat(request)
    data = (
        await compat.list_issues(order_id=order_id)
        if compat is not None
        else commerce_demo.list_buyer_issues(order_id)
    )
    return ApiResponse(success=True, message="Issues", data=data)


@fixture_router.get("/seller/issues", response_model=ApiResponse)
async def list_fixture_seller_issues(request: Request) -> ApiResponse:
    compat = _compat(request)
    data = (
        await compat.list_issues()
        if compat is not None
        else commerce_demo.list_seller_issues()
    )
    return ApiResponse(success=True, message="Issues", data=data)


router.include_router(fixture_router)
